import os
import math
import json
import re
import time
import logging
import xml.etree.ElementTree as ET
import concurrent.futures
import hmac
from collections import Counter
from urllib.parse import quote as urlquote

import requests
from flask import Flask, Response, render_template, request, jsonify

from logbooks import config as logcfg
from logbooks import net as lognet
from logbooks import sources as logsources

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TIMEOUT = 10
APP_VERSION = "BUILD_041"

# ─── Startup credential checks ────────────────────────────────────────────────
if not os.environ.get("HAMQTH_USER") or not os.environ.get("HAMQTH_PASS"):
    logger.warning("HamQTH: HAMQTH_USER or HAMQTH_PASS not set — HamQTH lookups will be skipped")

# Anthropic API (optional — used for notability checks on callsign history)
_anthropic_client = None
if os.environ.get("ANTHROPIC_API_KEY"):
    try:
        import anthropic as _anthropic_mod
        _anthropic_client = _anthropic_mod.Anthropic()
        logger.info("Anthropic API: configured")
    except ImportError:
        logger.warning("Anthropic API: anthropic package not installed — notability check disabled")
else:
    logger.info("Anthropic API: not configured (notability check disabled)")

# Module-level session key cache
_qrz_session_key = None
_hamqth_session_id = None

# Location blurb cache (in-memory, capped at 100 entries)
_location_blurb_cache: dict = {}
_LOCATION_BLURB_CACHE_MAX = 100

# Solar conditions cache (15-minute TTL)
_solar_cache: dict | None = None
_solar_cache_time: float = 0.0
SOLAR_CACHE_TTL = 900  # 15 minutes

# Log flag: emit raw POTA/SOTA JSON once so field names are visible in Docker logs


# Wikipedia API User-Agent (required per WMF API policy)
# Wikipedia and PSK Reporter ask for an identifying User-Agent. Self-hosters can add their own
# contact (an email or URL) with CONTACT_INFO so those services can reach them about their traffic.
PROJECT_URL = "https://github.com/KR4HBQ/ham-lookup"
_contact = os.environ.get("CONTACT_INFO", "").strip()
USER_AGENT = f"HamLookup/{APP_VERSION} (+{PROJECT_URL}" + (f"; {_contact}" if _contact else "") + ")"
WIKI_HEADERS = {'User-Agent': USER_AGENT}
lognet.set_user_agent(USER_AGENT)

_QUERY_RE = re.compile(r"\?[^\s'\")]+")


def _safe_err(e) -> str:
    """An exception as text with URL query strings removed.

    requests puts the request URL in its messages, and several services here take the API key,
    password or session id as a query parameter (Club Log, QRZ, HamQTH). This text is shown in the
    browser and in the logs, so the query string never goes with it.
    """
    return _QUERY_RE.sub("?[redacted]", str(e)) or type(e).__name__



# ─── Maidenhead Grid Utilities ────────────────────────────────────────────────

def grid_to_latlon(grid):
    """Convert Maidenhead grid square to (lat, lon) centroid."""
    if not grid or len(grid) < 2:
        return None, None
    g = grid.strip().upper()
    try:
        lon = (ord(g[0]) - 65) * 20 - 180
        lat = (ord(g[1]) - 65) * 10 - 90
        if len(g) >= 4:
            lon += int(g[2]) * 2
            lat += int(g[3])
            if len(g) >= 6:
                lon += (ord(g[4].lower()) - 97) * (5 / 60) + (2.5 / 60)
                lat += (ord(g[5].lower()) - 97) * (2.5 / 60) + (1.25 / 60)
            else:
                lon += 1.0
                lat += 0.5
        else:
            lon += 10.0
            lat += 5.0
    except Exception:
        return None, None
    return lat, lon


def get_grid_bounds(grid):
    """Return SW and NE corners of a 4-char grid square for map overlay."""
    if not grid or len(grid) < 4:
        return None
    g = grid.strip().upper()
    try:
        lon_sw = (ord(g[0]) - 65) * 20 - 180 + int(g[2]) * 2
        lat_sw = (ord(g[1]) - 65) * 10 - 90 + int(g[3])
        return {"sw": [lat_sw, lon_sw], "ne": [lat_sw + 1, lon_sw + 2]}
    except Exception:
        return None


def calc_bearing(from_grid, to_lat, to_lon):
    """Great-circle bearing + cardinal from operator grid to target station."""
    if not from_grid or to_lat is None or to_lon is None:
        return None, None
    from_lat, from_lon = grid_to_latlon(from_grid)
    if from_lat is None:
        return None, None
    phi1 = math.radians(from_lat)
    phi2 = math.radians(to_lat)
    dl = math.radians(to_lon - from_lon)
    x = math.sin(dl) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dl)
    brng = (math.degrees(math.atan2(x, y)) + 360) % 360
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return round(brng, 1), dirs[round(brng / 22.5) % 16]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    """Return great-circle distance between two lat/lon points in whole kilometres."""
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return round(R * 2 * math.asin(math.sqrt(a)))


# ─── Data Source Fetchers ─────────────────────────────────────────────────────

def fetch_callook(callsign):
    try:
        r = requests.get(f"https://callook.info/{callsign}/json", timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if data.get("status") == "VALID":
            return {"status": "ok", "data": data}
        return {"status": "not_found"}
    except Exception as e:
        return {"status": "error", "error": _safe_err(e)}


def _qrz_login():
    global _qrz_session_key
    user = os.environ.get("QRZ_USER")
    pw = os.environ.get("QRZ_PASS")
    if not user or not pw:
        return None
    try:
        r = requests.get(
            "https://xmldata.qrz.com/xml/current/",
            params={"username": user, "password": pw, "agent": "ham-lookup-1.0"},
            timeout=TIMEOUT,
        )
        root = ET.fromstring(r.text)
        ns = {"q": "http://xmldata.qrz.com"}
        key = root.find(".//q:Key", ns)
        if key is not None and key.text:
            _qrz_session_key = key.text
            return _qrz_session_key
    except Exception:
        pass
    return None


def fetch_qrz(callsign):
    global _qrz_session_key
    if not os.environ.get("QRZ_USER") or not os.environ.get("QRZ_PASS"):
        return {"status": "not_configured"}
    try:
        if not _qrz_session_key:
            _qrz_session_key = _qrz_login()
        if not _qrz_session_key:
            return {"status": "error", "error": "QRZ login failed"}

        r = requests.get(
            "https://xmldata.qrz.com/xml/current/",
            params={"s": _qrz_session_key, "callsign": callsign},
            timeout=TIMEOUT,
        )
        root = ET.fromstring(r.text)
        ns = {"q": "http://xmldata.qrz.com"}

        session_el = root.find(".//q:Session", ns)
        if session_el is not None:
            err = session_el.find("q:Error", ns)
            if err is not None and err.text and "session" in err.text.lower():
                _qrz_session_key = _qrz_login()
                if _qrz_session_key:
                    return fetch_qrz(callsign)
                return {"status": "error", "error": "QRZ session expired, refresh failed"}

        cs = root.find(".//q:Callsign", ns)
        if cs is None:
            return {"status": "not_found"}

        QRZ_FIELDS = [
            "call", "fname", "name", "addr1", "addr2", "state", "zip", "country",
            "lat", "lon", "grid", "county", "land", "class", "efdate", "expdate",
            "prev_call", "aliases", "lotw", "eqsl", "mqsl", "cqzone", "ituzone",
            "dxcc", "email", "image", "bio", "serial", "manager", "geoloc",
            "born", "timezone", "gmtoffset",
        ]
        data = {}
        for f in QRZ_FIELDS:
            v = cs.findtext(f"q:{f}", None, ns)
            if v is not None:
                data[f] = v

        # <bio> contains a relative URL path to the biography HTML page, e.g. /db/W1AW/bio
        # Fetch it with the session key, strip tags for plain text, and collect images.
        bio_path = data.get("bio", "")
        if bio_path and bio_path.startswith("/"):
            try:
                bio_r = requests.get(
                    f"https://www.qrz.com{bio_path}",
                    params={"s": _qrz_session_key},
                    timeout=TIMEOUT,
                )
                if bio_r.ok and bio_r.text:
                    bio_html = bio_r.text
                    # Collect images hosted on files.qrz.com
                    bio_images = re.findall(
                        r'<img[^>]+src=["\']([^"\']*files\.qrz\.com[^"\']*)["\']',
                        bio_html,
                        re.IGNORECASE,
                    )
                    data["bio_images"] = list(dict.fromkeys(bio_images))  # dedup, preserve order
            except Exception as bio_err:
                logger.debug(f"QRZ bio fetch failed for {callsign}: {_safe_err(bio_err)}")

        return {"status": "ok", "data": data}
    except Exception as e:
        return {"status": "error", "error": _safe_err(e)}


# HamQTH uses a fixed XML namespace for all responses
_HAMQTH_NS = {'h': 'https://www.hamqth.com'}


def _hq_text(el, tag):
    """Return text of a namespaced child element, or None."""
    if el is None:
        return None
    node = el.find(f'h:{tag}', _HAMQTH_NS)
    return node.text.strip() if node is not None and node.text else None


def _hamqth_login():
    global _hamqth_session_id
    user = os.environ.get("HAMQTH_USER")
    pw = os.environ.get("HAMQTH_PASS")
    if not user or not pw:
        logger.warning("HamQTH login skipped — HAMQTH_USER or HAMQTH_PASS not set")
        return None
    try:
        r = requests.get(
            "https://www.hamqth.com/xml.php",
            params={"u": user, "p": pw},
            timeout=TIMEOUT,
        )
        root = ET.fromstring(r.text)
        session_el = root.find('.//h:session', _HAMQTH_NS)
        key = _hq_text(session_el, 'session_id')
        if key:
            _hamqth_session_id = key
            logger.info("HamQTH session key obtained")
            return _hamqth_session_id
        err = _hq_text(session_el, 'error')
        logger.error(f"HamQTH login failed — key not found. Error: {err!r}")
        return None
    except Exception as e:
        logger.error(f"HamQTH login exception: {_safe_err(e)}")
        return None


def fetch_hamqth(callsign):
    global _hamqth_session_id
    if not os.environ.get("HAMQTH_USER") or not os.environ.get("HAMQTH_PASS"):
        return {"status": "not_configured"}
    try:
        if not _hamqth_session_id:
            _hamqth_session_id = _hamqth_login()
        if not _hamqth_session_id:
            return {"status": "error", "error": "session_failed",
                    "detail": "HamQTH login failed — check HAMQTH_USER/HAMQTH_PASS and server logs"}

        r = requests.get(
            "https://www.hamqth.com/xml.php",
            params={"id": _hamqth_session_id, "callsign": callsign, "prg": "ham-lookup"},
            timeout=TIMEOUT,
        )
        root = ET.fromstring(r.text)

        # Check for session error in the response and refresh once
        session_el = root.find('.//h:session', _HAMQTH_NS)
        if session_el is not None:
            err_text = _hq_text(session_el, 'error')
            if err_text and any(w in err_text.lower() for w in ("session", "wrong", "invalid", "expired")):
                logger.warning(f"HamQTH session error ({err_text!r}), refreshing…")
                _hamqth_session_id = _hamqth_login()
                if _hamqth_session_id:
                    return fetch_hamqth(callsign)
                return {"status": "error", "error": "session_failed", "detail": err_text}

        search_el = root.find('.//h:search', _HAMQTH_NS)
        if search_el is None:
            return {"status": "not_found"}

        FIELDS = [
            "callsign", "nick", "qth", "country", "adif", "itu", "cq", "grid",
            "adr_name", "adr_street1", "adr_city", "adr_zip", "adr_country",
            "us_state", "us_county", "district", "iota",
            "lotw", "qsldirect", "qsl", "eqsl",
            "email", "latitude", "longitude", "continent", "utc_offset",
            "picture", "web", "bio",
            "twitter", "facebook", "linkedin", "youtube", "skype",
        ]
        data = {tag: v for tag in FIELDS if (v := _hq_text(search_el, tag))}
        if not data:
            return {"status": "not_found"}
        return {"status": "ok", "data": data}
    except Exception as e:
        return {"status": "error", "error": _safe_err(e)}



def fetch_clublog(callsign):
    api_key = os.environ.get("CLUBLOG_API_KEY")
    if not api_key:
        return {"status": "not_configured"}
    try:
        r = requests.get(
            "https://clublog.org/dxcc",
            params={"api": api_key, "call": callsign, "full": "1"},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            return {"status": "error", "error": data["error"]}
        if not data.get("Name"):
            return {"status": "not_found"}
        return {"status": "ok", "data": data}
    except Exception as e:
        return {"status": "error", "error": _safe_err(e)}


# ─── Your log (worked before) ─────────────────────────────────────────────────
# The source log (LOG_SOURCE) is cached by logbooks.history; new QSOs go to every logger in LOG_WRITE.

_histories: dict = {}


def _history():
    """The History of the configured LOG_SOURCE, or None when no log is configured."""
    name = logcfg.source()
    if not name:
        return None
    if name not in _histories:
        _histories[name] = logsources.make_history(name)
    return _histories[name]


def _confirmed(q: dict) -> bool:
    return "Y" in (q.get("lotw_qsl_rcvd", "").upper(), q.get("qsl_rcvd", "").upper())


def log_callsign_qsos(callsign: str, records: list) -> list:
    """Return all QSOs with a specific callsign, newest first."""
    cs = callsign.upper()
    matched = [q for q in records if q.get("call", "").upper() == cs]
    matched.sort(key=lambda q: (q.get("qso_date", ""), q.get("time_on", "")), reverse=True)
    return [{
        "date":           q.get("qso_date", ""),
        "time":           q.get("time_on", ""),
        "band":           q.get("band", ""),
        "mode":           q.get("submode") or q.get("mode", ""),
        "rst_sent":       q.get("rst_sent", ""),
        "rst_rcvd":       q.get("rst_rcvd", ""),
        "lotw_qsl_sent":  q.get("lotw_qsl_sent", ""),
        "lotw_qsl_rcvd":  q.get("lotw_qsl_rcvd", ""),
        "eqsl_qsl_sent":  q.get("eqsl_qsl_sent", ""),
        "eqsl_qsl_rcvd":  q.get("eqsl_qsl_rcvd", ""),
        "confirmed":      _confirmed(q),
        "name":           q.get("name", ""),
    } for q in matched]


_CALL_RE = re.compile(r"^[A-Z0-9/]{2,15}$")


def log_top_contacts(records: list, limit: int = 10) -> list:
    """Return the top N most-worked callsigns."""
    counts: Counter = Counter()
    last_dates: dict = {}
    names: dict = {}
    for q in records:
        cs = q.get("call", "").upper()
        if cs and _CALL_RE.match(cs):
            counts[cs] += 1
            d = q.get("qso_date", "")
            if d > last_dates.get(cs, ""):
                last_dates[cs] = d
            if cs not in names:
                n = q.get("name", "").strip()
                if n:
                    names[cs] = n
    return [
        {"callsign": cs, "count": cnt, "last_date": last_dates.get(cs, ""), "name": names.get(cs, "")}
        for cs, cnt in counts.most_common(limit)
    ]


def log_check(callsign: str) -> dict:
    """Summary of the cached source log for one callsign, with its QSOs and the top contacts."""
    name = logcfg.source()
    info = {"source": name, "label": logcfg.label(name), "status": "not_configured",
            "error": "", "worked": False, "total_qsos": 0, "updated": 0,
            "has_confirmations": logsources.has_confirmations(name), "name": "",
            "qsos": [], "top_contacts": []}
    if not name:
        info["error"] = "set LOG_SOURCE (and that logger's settings) to check your log"
        return info
    missing = logcfg.read_missing(name)
    if missing:
        info["error"] = "set " + ", ".join(missing)
        return info
    hist = _history()
    snap = hist.get()
    qsos = log_callsign_qsos(callsign, snap.records)
    info.update(
        status="ok" if snap.records or (snap.updated and not snap.error) else
               ("loading" if snap.loading else "error"),
        error=snap.error, worked=bool(qsos), total_qsos=len(snap.records), updated=snap.updated,
        name=next((q["name"] for q in qsos if q["name"]), ""),
        qsos=qsos, top_contacts=log_top_contacts(snap.records),
    )
    return info


def fetch_hamdb(callsign):
    try:
        r = requests.get(
            f"https://api.hamdb.org/{callsign}/json/ham-lookup-app",
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        d = r.json()
        inner = d.get("hamdb", {}).get("callsign", {})
        if not inner or inner.get("call", "").upper() != callsign.upper():
            return {"status": "not_found"}
        return {"status": "ok", "data": inner}
    except Exception as e:
        return {"status": "error", "error": _safe_err(e)}


def fetch_allstarlink(callsign):
    """Query AllStarLink node directory (pipe/space-delimited flat file) for this callsign."""
    try:
        r = requests.get(
            "https://allmondb.allstarlink.org/allmondb.php",
            timeout=5,
            headers={"User-Agent": "ham-lookup/1.0"},
        )
        r.raise_for_status()

        raw_text = r.text.strip()
        logger.info(f"AllStarLink raw response (first 500 chars): {repr(raw_text[:500])}")

        cs_upper = callsign.upper()
        nodes_found = []
        parsed_any = False

        for line in raw_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            # Try pipe-delimited first (most common allmondb format)
            if "|" in line:
                parts = [p.strip() for p in line.split("|")]
                parsed_any = True
                if len(parts) >= 2:
                    node_id = parts[0]
                    node_cs = parts[1].upper()
                    base_cs = re.split(r"[/\-]", node_cs)[0]
                    desc = parts[2] if len(parts) > 2 else ""
                    if node_cs == cs_upper or base_cs == cs_upper:
                        nodes_found.append({
                            "node_id": node_id, "callsign": node_cs, "description": desc
                        })
                continue

            # Fallback: space-delimited
            parts = line.split()
            if len(parts) >= 2:
                parsed_any = True
                node_id = parts[0]
                node_cs = parts[1].upper()
                base_cs = re.split(r"[/\-]", node_cs)[0]
                desc = " ".join(parts[2:]) if len(parts) > 2 else ""
                if node_cs == cs_upper or base_cs == cs_upper:
                    nodes_found.append({
                        "node_id": node_id, "callsign": node_cs, "description": desc
                    })

        if not parsed_any:
            sample = raw_text[:200]
            logger.warning(f"AllStarLink: unrecognised response format. Sample: {repr(sample)}")
            return {
                "status": "error",
                "error": "format_unknown",
                "nodes": [],
                "raw_sample": sample,
            }

        if not nodes_found:
            return {"status": "not_found", "data": []}

        # Fetch live stats for each matched node (cap at 5, short timeout)
        enriched = []
        for node in nodes_found[:5]:
            nid = node.get("node_id")
            result = dict(node)
            if nid:
                try:
                    sr = requests.get(
                        f"https://stats.allstarlink.org/api/stats/{nid}",
                        timeout=5,
                    )
                    if sr.ok:
                        result["live"] = sr.json()
                        result["online"] = result["live"].get("stats") is not None
                    else:
                        result["online"] = False
                except Exception:
                    result["online"] = False
            enriched.append(result)

        return {"status": "ok", "data": enriched}

    except requests.exceptions.Timeout:
        logger.info("AllStarLink DB timeout — marking unavailable")
        return {"status": "unavailable", "data": []}
    except requests.exceptions.HTTPError as e:
        logger.info(f"AllStarLink HTTP error ({_safe_err(e)}) — marking unavailable")
        return {"status": "unavailable", "data": []}
    except Exception as e:
        return {"status": "error", "error": _safe_err(e)}


def fetch_pskreporter(callsign):
    try:
        r = requests.get(
            "https://www.pskreporter.info/cgi-bin/pskquery5.pl",
            params={
                "encap": "0",
                "callback": "doNothing",
                "statistics": "0",
                "noactive": "1",
                "nolocator": "1",
                "lastsecondsZ": "3600",
                "fCallsign": callsign,
            },
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT,
        )
        text = r.text.strip()
        m = re.match(r"^doNothing\((.*)\);?\s*$", text, re.DOTALL)
        if m:
            text = m.group(1)
        data = json.loads(text)
        reports = data.get("receptionReport", [])
        if isinstance(reports, dict):
            reports = [reports]
        return {"status": "ok", "data": reports[:10]}
    except Exception as e:
        return {"status": "error", "error": _safe_err(e)}


def fetch_weather(lat, lon):
    if lat is None or lon is None:
        return {"status": "skipped"}
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "current_weather": "true",
                "temperature_unit": "fahrenheit",
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        cw = r.json().get("current_weather", {})
        WMO = {
            0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
            45: "Fog", 48: "Depositing rime fog", 51: "Light drizzle",
            53: "Moderate drizzle", 55: "Dense drizzle", 61: "Slight rain",
            63: "Moderate rain", 65: "Heavy rain", 71: "Slight snow",
            73: "Moderate snow", 75: "Heavy snow", 77: "Snow grains",
            80: "Slight showers", 81: "Moderate showers", 82: "Violent showers",
            85: "Slight snow showers", 86: "Heavy snow showers",
            95: "Thunderstorm", 96: "Thunderstorm w/ slight hail",
            99: "Thunderstorm w/ heavy hail",
        }
        dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
        cw["description"] = WMO.get(cw.get("weathercode", -1), f"Code {cw.get('weathercode')}")
        cw["wind_cardinal"] = dirs[round((cw.get("winddirection", 0)) / 22.5) % 16]
        return {"status": "ok", "data": cw}
    except Exception as e:
        return {"status": "error", "error": _safe_err(e)}


def fetch_timezone(lat, lon):
    if lat is None or lon is None:
        return {"status": "skipped"}
    try:
        from timezonefinder import TimezoneFinder
        tz = TimezoneFinder().timezone_at(lat=lat, lng=lon)
        if tz:
            return {"status": "ok", "data": {"timezone": tz}}
        return {"status": "error", "error": "Timezone not found for coordinates"}
    except ImportError:
        return {"status": "not_configured", "error": "timezonefinder not installed"}
    except Exception as e:
        return {"status": "error", "error": _safe_err(e)}


def fetch_location_blurb(city: str, state: str, country: str) -> list:
    """Return a bulleted list of interesting facts about the operator's location.

    Uses Claude Haiku.  Results are cached in-memory (max _LOCATION_BLURB_CACHE_MAX
    entries, keyed by lower-cased "{city},{country}").  Returns [] silently on any
    error or when Anthropic is not configured.
    """
    if not _anthropic_client:
        return []
    city    = (city    or "").strip()
    state   = (state   or "").strip()
    country = (country or "").strip()
    if not city and not country:
        return []

    cache_key = f"{city.lower()},{country.lower()}"
    if cache_key in _location_blurb_cache:
        return _location_blurb_cache[cache_key]

    location_str = ", ".join(p for p in [city, state, country] if p)
    try:
        prompt = (
            f"What is most notable about {location_str}?\n"
            f"Respond with EXACTLY 3 to 5 bullet points.\n"
            f"Each bullet point must be 15 words or fewer.\n"
            f"Start every line with •.\n"
            f"No introduction. No conclusion. Nothing else.\n"
            f"Example:\n"
            f"• Home of Arizona State University, founded 1885\n"
            f"• Sits along the Salt River, known for Tempe Town Lake\n"
            f"• Major tech hub hosting Intel, PayPal, and Cisco operations"
        )
        response = _anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = (response.content[0].text or "").strip()
        lines = []
        if any(c in text for c in '•-*·'):
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                # Strip bullet chars and leading numbering (1. 2) etc.)
                line = re.sub(r'^[\s\-*•·]*\d*[\.\):\s]*', '', line).strip()
                line = re.sub(r'^[-*•·]\s*', '', line).strip()
                if line:
                    lines.append(line)
        else:
            # Fallback: no bullets found — split on blank lines
            for para in re.split(r'\n{2,}', text):
                para = para.strip()
                if para:
                    lines.append(para)
        # Evict oldest entry if cache is full
        if len(_location_blurb_cache) >= _LOCATION_BLURB_CACHE_MAX:
            oldest = next(iter(_location_blurb_cache))
            del _location_blurb_cache[oldest]
        _location_blurb_cache[cache_key] = lines
        logger.info(f"Location blurb for {location_str!r}: {len(lines)} bullet(s)")
        return lines
    except Exception as e:
        logger.debug(f"Location blurb failed for {location_str!r}: {_safe_err(e)}")
        return []


# ─── ECR Coasters ─────────────────────────────────────────────────────────────

def get_ecr_coasters() -> list:
    """Read ecr_coasters.json on every call (supports live edits without restart)."""
    try:
        with open("ecr_coasters.json") as f:
            return [c.upper().strip() for c in json.load(f) if c.strip()]
    except Exception as e:
        logger.warning(f"ECR coasters file error: {_safe_err(e)}")
        return []


# Module-level flag so we log the notable DB size only once
_notable_callsigns_logged = False


def get_notable_callsigns() -> dict:
    """Read notable_callsigns.json on every request (supports live edits without restart).

    Keys are uppercased callsigns. Returns {} silently when the file is absent.
    Logs the entry count once per process start on the first successful load.
    """
    global _notable_callsigns_logged
    try:
        with open("notable_callsigns.json") as f:
            data = json.load(f)
        if not _notable_callsigns_logged:
            logger.info(f"Notable callsigns DB: {len(data)} entries")
            _notable_callsigns_logged = True
        return {k.upper(): v for k, v in data.items()}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"notable_callsigns.json error: {_safe_err(e)}")
        return {}


# ─── Notability & Callsign History ───────────────────────────────────────────

def _is_current_holder(notable_entry: dict | None, current_name: str) -> bool:
    """Return True if the notable_entry person is plausibly the current callsign holder.

    Checks whether any word of 4+ characters from the notable person's name
    (known_as preferred) appears in the current holder's name string.
    Returns True when inconclusive (no meaningful words to compare) so the
    banner defaults to treating the notable as the current holder.
    """
    if not notable_entry or not current_name:
        return False
    notable_name = (
        notable_entry.get("fcc_name") or notable_entry.get("known_as") or notable_entry.get("name") or ""
    ).lower()
    current = current_name.lower()
    words = [w for w in notable_name.split() if len(w) >= 4]
    if not words:
        return True  # inconclusive — default to current holder
    return any(w in current for w in words)


def _name_matches_fuzzy(fcc_name: str, wiki_title: str) -> bool:
    """Return True when fcc_name plausibly refers to the same person as wiki_title.

    Handles:
    - Middle initials stripped (JOSEPH F WALSH → joseph walsh)
    - Common formal→nickname mappings (JOSEPH → Joe, WILLIAM → Bill, …)
    - Reverse nickname→formal matching
    - First-letter prefix fallback
    """
    def _norm(s: str) -> list[str]:
        return re.sub(r'[^a-z0-9 ]', '', s.lower()).split()

    fcc_words  = [w for w in _norm(fcc_name)  if len(w) > 1]  # strip middle initials
    wiki_words = _norm(wiki_title)

    if not fcc_words or not wiki_words:
        return False

    fcc_last = fcc_words[-1]
    if fcc_last not in wiki_words:
        return False

    NICKNAMES: dict[str, list[str]] = {
        'joseph':      ['joe', 'jo'],
        'william':     ['bill', 'will', 'billy', 'willie'],
        'robert':      ['bob', 'rob', 'bobby'],
        'richard':     ['dick', 'rick', 'rich'],
        'james':       ['jim', 'jimmy', 'jamie'],
        'charles':     ['chuck', 'charlie', 'chas'],
        'michael':     ['mike', 'mick', 'mickey'],
        'thomas':      ['tom', 'tommy'],
        'john':        ['jack', 'johnny', 'jon'],
        'edward':      ['ed', 'eddie', 'ned'],
        'george':      ['georgie'],
        'david':       ['dave', 'davy'],
        'donald':      ['don', 'donnie'],
        'ronald':      ['ron', 'ronnie'],
        'kenneth':     ['ken', 'kenny'],
        'lawrence':    ['larry', 'laurie'],
        'gerald':      ['jerry', 'gerry'],
        'anthony':     ['tony'],
        'stephen':     ['steve'],
        'steven':      ['steve'],
        'christopher': ['chris'],
        'matthew':     ['matt'],
        'andrew':      ['andy', 'drew'],
        'daniel':      ['dan', 'danny'],
        'timothy':     ['tim', 'timmy'],
        'patrick':     ['pat', 'paddy'],
        'peter':       ['pete'],
        'walter':      ['walt'],
        'harold':      ['hal', 'harry'],
        'henry':       ['hank', 'harry'],
        'frederick':   ['fred', 'freddy'],
        'albert':      ['al', 'bert'],
        'arthur':      ['art'],
        'eugene':      ['gene'],
        'ernest':      ['ernie'],
        'clarence':    ['claire'],
        'raymond':     ['ray'],
        'samuel':      ['sam'],
        'benjamin':    ['ben', 'benny'],
        'nathaniel':   ['nate', 'nat'],
        'theodore':    ['ted', 'theo'],
        'leonard':     ['len', 'lenny'],
        'stanley':     ['stan'],
    }

    fcc_first        = fcc_words[0].lower() if len(fcc_words) > 1 else ''
    wiki_first_words = [w for w in wiki_words[:-1]]  # everything except last word

    if not fcc_first:
        # Only last name provided — treat as match if last name already matched
        return True

    # Direct first-letter prefix match
    if any(w.startswith(fcc_first[0]) for w in wiki_first_words if w):
        return True

    # Formal → nickname  (JOSEPH → joe)
    for nick in NICKNAMES.get(fcc_first, []):
        if nick in wiki_first_words:
            return True

    # Nickname → formal  (JOE → joseph)
    for formal, nicks in NICKNAMES.items():
        if fcc_first in nicks:
            if formal in wiki_first_words or any(
                w.startswith(formal[0]) for w in wiki_first_words
            ):
                return True

    return False


def _build_wiki_query(name: str) -> tuple[str, str]:
    """Return (primary_query, fallback_query) for a Wikipedia name search.

    Primary appends 'amateur radio' as context; fallback is clean name only.
    Middle initials (single chars after title-casing) are stripped.
    """
    parts = [p for p in name.title().split() if len(p) > 1]
    name_clean = ' '.join(parts) if parts else name.strip()
    return f"{name_clean} amateur radio", name_clean


def _check_wikipedia(name: str) -> dict:
    """Search Wikipedia for a person by name; return notability info if a match is found.

    Two-stage search: primary query adds 'amateur radio' context; fallback is plain name.
    All requests carry WIKI_HEADERS (User-Agent required by WMF API policy).
    Checks up to 3 results per search for a fuzzy name match.
    """
    if not name or not name.strip():
        return {"notable": False}

    primary_query, fallback_query = _build_wiki_query(name)

    def _search(query: str) -> str | None:
        """Return first Wikipedia title that fuzzy-matches name, or None."""
        try:
            sr = requests.get(
                "https://en.wikipedia.org/w/api.php",
                params={"action": "query", "list": "search", "srsearch": query,
                        "format": "json", "srlimit": 3, "utf8": 1},
                headers=WIKI_HEADERS,
                timeout=8,
            )
            for hit in sr.json().get("query", {}).get("search", []):
                title = hit.get("title", "")
                if _name_matches_fuzzy(name, title):
                    return title
        except Exception as e:
            logger.debug(f"Wikipedia search failed for {query!r}: {_safe_err(e)}")
        return None

    title = _search(primary_query) or _search(fallback_query)
    if title is None:
        return {"notable": False}

    try:
        sumr = requests.get(
            f"https://en.wikipedia.org/api/rest_v1/page/summary/{urlquote(title, safe='')}",
            headers=WIKI_HEADERS,
            timeout=8,
        )
        if not sumr.ok:
            return {"notable": False}
        d = sumr.json()
        return {
            "notable":           True,
            "reason":            "",
            "profession":        d.get("description", ""),
            "wikipedia_extract": d.get("extract", ""),
            "wikipedia_image":   (d.get("thumbnail") or {}).get("source"),
            "wikipedia_url":     ((d.get("content_urls") or {}).get("desktop") or {}).get("page"),
            "known_as":          "",
        }
    except Exception as e:
        logger.debug(f"Wikipedia summary failed for {title!r}: {_safe_err(e)}")
        return {"notable": False}


def _check_notability_claude(name: str) -> dict:
    """Use Claude Haiku to assess notability when Wikipedia has no result.

    Prompt handles formal FCC names (e.g. JOSEPH F WALSH) and asks for a
    'known_as' field when the common name differs from the legal name.
    """
    if not _anthropic_client:
        return {"notable": False}
    try:
        prompt = (
            f"Is the person named '{name}' a notable public figure?\n"
            f"This may be a formal legal name (e.g. 'JOSEPH F WALSH' could be "
            f"Joe Walsh the rock musician). Consider common nicknames and "
            f"middle initials when evaluating.\n"
            f"Answer in JSON only, no other text:\n"
            f'{{"notable": true or false, "reason": "one sentence or empty string", '
            f'"profession": "profession or empty string", '
            f'"known_as": "common name if different from input, or empty string"}}'
        )
        response = _anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = (response.content[0].text or "").strip()
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            data = json.loads(m.group())
            if data.get("notable"):
                return {
                    "notable":           True,
                    "reason":            data.get("reason", ""),
                    "profession":        data.get("profession", ""),
                    "known_as":          data.get("known_as", ""),
                    "wikipedia_extract": None,
                    "wikipedia_image":   None,
                    "wikipedia_url":     None,
                }
    except Exception as e:
        logger.debug(f"Claude notability check failed for {name!r}: {_safe_err(e)}")
    return {"notable": False}


def _check_notability(name: str) -> dict:
    """Run Wikipedia check; fall back to Claude Haiku if no Wikipedia result found."""
    if not name or not name.strip():
        return {"notable": False}
    result = _check_wikipedia(name)
    if result.get("notable"):
        logger.info(f"Notability check for {name!r}: notable=True, source=wikipedia")
        return result
    result = _check_notability_claude(name)
    if result.get("notable"):
        logger.info(f"Notability check for {name!r}: notable=True, source=claude")
    else:
        logger.info(f"Notability check for {name!r}: notable=False, source=none")
    return result


_fcc_unavailable_logged = False


def fetch_fcc_history(callsign: str) -> dict:
    """Fetch callsign license history from FCC ULS basicSearch API.

    The FCC License View API has been refusing requests (HTTP 403 after a redirect to www.fcc.gov,
    or an HTML page instead of JSON). That is reported as "unavailable", logged once per process.
    """
    global _fcc_unavailable_logged
    try:
        r = requests.get(
            "https://data.fcc.gov/api/license-view/basicSearch/getLicenses",
            params={"format": "json", "searchValue": callsign, "statusDesc": "Any"},
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=3,
        )
        try:
            data = r.json() if r.status_code == 200 else None
        except ValueError:
            data = None
        if not isinstance(data, dict):
            if not _fcc_unavailable_logged:
                logger.warning(f"FCC ULS history unavailable: HTTP {r.status_code} from {r.url.split('?')[0]}"
                               " (the FCC License View API is refusing requests; logged once)")
                _fcc_unavailable_logged = True
            return {"status": "unavailable", "error": f"FCC answered HTTP {r.status_code}", "licenses": []}
        licenses = (data.get("Licenses") or {}).get("License", [])
        if isinstance(licenses, dict):
            licenses = [licenses]
        if not licenses:
            return {"status": "not_found", "licenses": []}
        return {
            "status": "ok",
            "licenses": [{
                "name":         lic.get("licenseeName", "").strip(),
                "status":       lic.get("statusDesc", ""),
                "grant_date":   lic.get("grantDate", ""),
                "expired_date": lic.get("expiredDate", ""),
            } for lic in licenses],
        }
    except requests.exceptions.Timeout:
        logger.info(f"FCC ULS history timeout for {callsign}")
        return {"status": "timeout", "licenses": []}
    except Exception as e:
        logger.error(f"FCC ULS history error for {callsign}: {_safe_err(e)}")
        return {"status": "error", "error": _safe_err(e), "licenses": []}


def fetch_callsign_history(callsign: str, current_name: str = "",
                           notable_entry=None) -> dict:
    """Fetch FCC license history + check current holder notability concurrently.

    current_name:  best available operator name from QRZ/HamQTH/Callook/HamDB.
    notable_entry: if provided from notable_callsigns.json, the JSON entry is
                   authoritative — return immediately with its data and skip all
                   Wikipedia, Claude, and FCC lookups entirely.
    """
    name_to_check = current_name.strip()

    # SHORT-CIRCUIT: notable_callsigns.json entry is authoritative.
    # Return immediately — no Wikipedia search, no Claude API call, no FCC lookup.
    if notable_entry is not None:
        current_notable: dict = {
            "name":              notable_entry.get("name", ""),
            "known_as":          notable_entry.get("known_as", ""),
            "notable":           True,
            "reason":            notable_entry.get("note", ""),
            "profession":        notable_entry.get("profession", ""),
            "wikipedia_extract": None,
            "wikipedia_image":   None,
            "wikipedia_url":     notable_entry.get("wikipedia", None),
            "source":            "notable_db",
            "entry_type":        notable_entry.get("type", "person"),
            "icon":              notable_entry.get("icon", "⭐"),
            "is_current_holder": _is_current_holder(notable_entry, name_to_check),
        }
        logger.info(
            f"Notable DB hit for {callsign}: {current_notable['name']!r}"
            f" ({current_notable['entry_type']}) — skipping Wikipedia/Claude/FCC"
        )
        return {
            "status":           "ok",
            "current_notable":  current_notable,
            "previous_holders": [],
        }

    # Run FCC fetch and current-name notability check concurrently
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        fcc_fut     = ex.submit(fetch_fcc_history, callsign)
        notable_fut = ex.submit(_check_notability, name_to_check) if name_to_check else None

        try:
            fcc = fcc_fut.result(timeout=5)
        except Exception:
            fcc = {"status": "timeout", "licenses": []}

        if notable_fut is not None:
            try:
                nd = notable_fut.result(timeout=12)
            except Exception:
                nd = {"notable": False}
            current_notable = {"name": name_to_check, "is_current_holder": True, **nd}
        else:
            current_notable = None

    # If FCC is unavailable, return partial result — current_notable still populated
    if fcc["status"] != "ok":
        return {
            "status":           fcc["status"],
            "current_notable":  current_notable,
            "previous_holders": [],
        }

    licenses = fcc["licenses"]
    if not licenses:
        return {
            "status":           "not_found",
            "current_notable":  current_notable,
            "previous_holders": [],
        }

    active           = next((l for l in licenses if "active" in l["status"].lower()), None)
    historical       = [l for l in licenses if "active" not in l["status"].lower()]
    fcc_current_name = active["name"].strip() if active else ""

    # If no external name, fall back to FCC active-license name
    if current_notable is None and fcc_current_name:
        nd = _check_notability(fcc_current_name)
        current_notable = {"name": fcc_current_name, "is_current_holder": True, **nd}

    # Collect unique historical holder names and check notability in parallel
    hist_names: list = []
    seen_upper: set  = set()
    for l in historical:
        nu = l["name"].strip().upper()
        if nu and nu not in seen_upper:
            hist_names.append(l["name"].strip())
            seen_upper.add(nu)

    notability: dict = {}
    if hist_names:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(hist_names), 6)) as nx:
            fut_map = {nx.submit(_check_notability, name): name for name in hist_names}
            done, not_done = concurrent.futures.wait(fut_map, timeout=8)
            for f in done:
                name = fut_map[f]
                try:
                    notability[name] = f.result()
                except Exception:
                    notability[name] = {"notable": False}
            for f in not_done:
                notability[fut_map[f]] = {"notable": False}
                f.cancel()

    prev_results: list = []
    prev_seen:    set  = set()
    for lic in historical:
        name = lic.get("name", "").strip()
        nu   = name.upper()
        if not name or nu in prev_seen:
            continue
        prev_seen.add(nu)
        nd = notability.get(name, {"notable": False})
        prev_results.append({
            "name":         name,
            "status":       lic["status"],
            "grant_date":   lic["grant_date"],
            "expired_date": lic["expired_date"],
            **nd,
        })

    return {
        "status":           "ok",
        "current_notable":  current_notable,
        "previous_holders": prev_results,
    }


# ─── Solar Conditions ────────────────────────────────────────────────────────

def get_solar_conditions() -> dict | None:
    """Fetch solar-flux, K/A indices from the HamQSL XML feed.

    Cached for 15 minutes (module-level).  Returns None on any error.
    """
    global _solar_cache, _solar_cache_time
    now = time.time()
    if _solar_cache is not None and (now - _solar_cache_time) < SOLAR_CACHE_TTL:
        return _solar_cache
    try:
        r = requests.get("https://www.hamqsl.com/solarxml.php", timeout=5)
        r.raise_for_status()
        root = ET.fromstring(r.text)
        sd = root.find("solardata")
        if sd is None:
            return None

        def _txt(tag: str) -> str | None:
            el = sd.find(tag)
            return el.text.strip() if el is not None and el.text else None

        data = {
            "solarflux": _txt("solarflux"),
            "aindex":    _txt("aindex"),
            "kindex":    _txt("kindex"),
            "xray":      _txt("xray"),
            "sunspots":  _txt("sunspots"),
        }
        _solar_cache      = data
        _solar_cache_time = now
        logger.info(
            f"Solar conditions updated: SFI={data['solarflux']} "
            f"K={data['kindex']} A={data['aindex']}"
        )
        return data
    except Exception as e:
        logger.debug(f"Solar conditions fetch failed: {_safe_err(e)}")
        return None


# ─── POTA / SOTA Portable Operations ─────────────────────────────────────────

def get_portable_ops(callsign: str) -> dict:
    """Fetch POTA stats via the public /stats/user/{call} endpoint.

    Single request — no auth required. SOTA removed (API ToS prohibits
    AI-generated clients). Live spot removed (requires auth).
    Returns None for all keys on failure — never raises.
    """
    cs = callsign.upper()
    try:
        r = requests.get(
            f"https://api.pota.app/stats/user/{cs}", timeout=5
        )
        r.raise_for_status()
        data = r.json()
        activator = data.get("activator") or {}
        hunter    = data.get("hunter") or {}
        pota_activator = {
            "activations": activator.get("activations"),
            "parks":       activator.get("parks"),
            "qsos":        activator.get("qsos"),
        } if activator else None
        pota_hunter = {
            "parks_hunted": hunter.get("parks"),
            "qsos":         hunter.get("qsos"),
        } if hunter else None
    except Exception as e:
        logger.debug(f"POTA stats error for {cs}: {_safe_err(e)}")
        pota_activator, pota_hunter = None, None

    return {
        "pota_activator": pota_activator,
        "pota_hunter":    pota_hunter,
        "pota_spot":      None,
        "sota_activator": None,
        "sota_chaser":    None,
    }

# ─── Optional password ────────────────────────────────────────────────────────
# With HAMLOOKUP_PASSWORD set, every page and API call asks for it (HTTP Basic auth: the browser
# shows its own login box and remembers it). Any user name is accepted. Use HTTPS (a reverse proxy
# or Cloudflare) when the page is reachable from outside: Basic auth sends the password with every
# request.

@app.before_request
def _require_password():
    password = os.environ.get("HAMLOOKUP_PASSWORD", "")
    if not password:
        return None
    auth = request.authorization
    given = (auth.password or "") if auth and auth.type == "basic" else ""
    if given and hmac.compare_digest(given.encode("utf-8"), password.encode("utf-8")):
        return None
    if given:
        logger.warning("Wrong Ham Lookup password from %s", request.remote_addr)
        time.sleep(1)  # slows down guessing
    return Response("Password required", 401, {"WWW-Authenticate": 'Basic realm="Ham Lookup", charset="UTF-8"'})


# ─── Flask Routes ─────────────────────────────────────────────────────────────

def _log_url() -> str:
    """Where "View full log" points: the Wavelog or Cloudlog web UI, when there is one."""
    if logcfg.source() == "wavelog":
        return logcfg.env("WAVELOG_URL")
    for name in logcfg.writers():
        if name in ("wavelog", "cloudlog"):
            return logcfg.env(f"{name.upper()}_URL")
    return ""


@app.route("/")
def index():
    return render_template(
        "index.html",
        log_url=_log_url(),
        log_source_label=logcfg.label(logcfg.source()) if logcfg.source() else "",
        log_writers=[{"id": n, "label": logcfg.label(n)} for n in logcfg.writers()],
        app_version=APP_VERSION,
        maptiler_key=os.environ.get("MAPTILER_API_KEY", ""),
    )


@app.route("/lookup", methods=["POST"])
def lookup():
    body = request.get_json(silent=True) or {}
    callsign = body.get("callsign", "").strip().upper()
    if not callsign or not re.match(r"^[A-Z0-9/]{2,15}$", callsign):
        return jsonify({"error": "Invalid callsign"}), 400

    my_grid = os.environ.get("MY_GRID", "")

    # Notable callsigns DB lookup (silent degradation if file absent)
    notable_db    = get_notable_callsigns()
    notable_entry = notable_db.get(callsign.upper())

    tasks = {
        "callook":       lambda: fetch_callook(callsign),
        "qrz":           lambda: fetch_qrz(callsign),
        "hamqth":        lambda: fetch_hamqth(callsign),
        "clublog":       lambda: fetch_clublog(callsign),
        "log":           lambda: log_check(callsign),
        "pskreporter":   lambda: fetch_pskreporter(callsign),
        "allstarlink":   lambda: fetch_allstarlink(callsign),
        "hamdb":         lambda: fetch_hamdb(callsign),
    }

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        futures = {executor.submit(fn): key for key, fn in tasks.items()}
        done, not_done = concurrent.futures.wait(futures, timeout=11)
        for f in done:
            key = futures[f]
            try:
                results[key] = f.result()
            except Exception as e:
                results[key] = {"status": "error", "error": _safe_err(e)}
        for f in not_done:
            results[futures[f]] = {"status": "error", "error": "Timeout after 11s"}
            f.cancel()

    # Derive best lat/lon/grid — priority: QRZ > HamQTH > Callook
    lat = lon = grid = None

    def _flt(v):
        try:
            f = float(v or 0)
            return f if f != 0.0 else None
        except Exception:
            return None

    if results.get("qrz", {}).get("status") == "ok":
        d = results["qrz"]["data"]
        lat = _flt(d.get("lat"))
        lon = _flt(d.get("lon"))
        grid = d.get("grid")

    if lat is None and results.get("hamqth", {}).get("status") == "ok":
        d = results["hamqth"]["data"]
        lat = _flt(d.get("latitude"))
        lon = _flt(d.get("longitude"))
        grid = grid or d.get("grid")

    if lat is None and results.get("callook", {}).get("status") == "ok":
        d = results["callook"]["data"]
        try:
            loc = d.get("location", {})
            lat = _flt(loc.get("latitude"))
            lon = _flt(loc.get("longitude"))
        except Exception:
            pass
        grid = grid or (d.get("otherInfo") or {}).get("gridSquare")

    if lat is None and results.get("hamdb", {}).get("status") == "ok":
        d = results["hamdb"]["data"]
        lat = _flt(d.get("lat"))
        lon = _flt(d.get("lon"))
        grid = grid or d.get("grid")

    if grid and lat is None:
        lat, lon = grid_to_latlon(grid)

    # Derive best available operator name for notability check (priority: QRZ > HamQTH > Callook > HamDB)
    _best_name = ""
    if results.get("qrz", {}).get("status") == "ok":
        _qd = results["qrz"]["data"]
        _best_name = f"{_qd.get('fname', '')} {_qd.get('name', '')}".strip()
    if not _best_name and results.get("hamqth", {}).get("status") == "ok":
        _best_name = (results["hamqth"]["data"].get("adr_name") or "").strip()
    if not _best_name and results.get("callook", {}).get("status") == "ok":
        _best_name = (results["callook"]["data"].get("name") or "").strip()
    if not _best_name and results.get("hamdb", {}).get("status") == "ok":
        _hd = results["hamdb"]["data"]
        _best_name = f"{_hd.get('fname', '')} {_hd.get('name', '')}".strip()

    # Derive city/state/country for location blurb (from phase-1 results)
    _qd2 = results.get("qrz",    {}).get("data") or {}
    _hd2 = results.get("hamqth", {}).get("data") or {}
    _dd2 = results.get("hamdb",  {}).get("data") or {}
    _blurb_city    = (_qd2.get("addr2")    or _hd2.get("adr_city")  or _dd2.get("addr2")   or "").strip()
    _blurb_state   = (_qd2.get("state")    or _hd2.get("us_state")  or _dd2.get("state")   or "").strip()
    _blurb_country = (_qd2.get("country")  or _hd2.get("country")   or _dd2.get("country") or "").strip()

    # Whether the notable DB entry refers to the current callsign holder
    _notable_is_current = _is_current_holder(notable_entry, _best_name)

    # Phase 2: weather, timezone, callsign history, location blurb, portable ops run concurrently
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ph2_ex:
        wx_fut      = ph2_ex.submit(fetch_weather,          lat, lon)
        tz_fut      = ph2_ex.submit(fetch_timezone,         lat, lon)
        hist_fut    = ph2_ex.submit(fetch_callsign_history, callsign, _best_name, notable_entry)
        blurb_fut   = (
            ph2_ex.submit(fetch_location_blurb, _blurb_city, _blurb_state, _blurb_country)
            if (lat is not None and _anthropic_client and (_blurb_city or _blurb_country))
            else None
        )
        portops_fut = ph2_ex.submit(get_portable_ops, callsign)

        try:
            results["weather"]  = wx_fut.result(timeout=12)
        except Exception:
            results["weather"]  = {"status": "error", "error": "timeout"}
        try:
            results["timezone"] = tz_fut.result(timeout=12)
        except Exception:
            results["timezone"] = {"status": "error", "error": "timeout"}
        try:
            callsign_history = hist_fut.result(timeout=25)
        except Exception:
            # Preserve notable_entry in the fallback so the banner can still render
            _cn_fallback = None
            if notable_entry is not None:
                _cn_fallback = {
                    "name":              notable_entry.get("name", ""),
                    "known_as":          notable_entry.get("known_as", ""),
                    "notable":           True,
                    "reason":            notable_entry.get("note", ""),
                    "profession":        notable_entry.get("profession", ""),
                    "wikipedia_extract": None,
                    "wikipedia_image":   None,
                    "wikipedia_url":     notable_entry.get("wikipedia", None),
                    "source":            "notable_db",
                    "entry_type":        notable_entry.get("type", "person"),
                    "icon":              notable_entry.get("icon", "⭐"),
                    "is_current_holder": _notable_is_current,
                }
            callsign_history = {
                "status":           "timeout",
                "current_notable":  _cn_fallback,
                "previous_holders": [],
            }
        if blurb_fut is not None:
            try:
                location_blurb = blurb_fut.result(timeout=12)
            except Exception:
                location_blurb = []
        else:
            location_blurb = []

        try:
            portable_ops = portops_fut.result(timeout=8)
        except Exception:
            portable_ops = {
                "pota_activator": None, "pota_hunter":    None,
                "pota_spot":      None, "sota_activator": None,
                "sota_chaser":    None,
            }

    hdg, card   = calc_bearing(my_grid, lat, lon)
    solar       = get_solar_conditions()

    # Grid-to-grid distance (km) — requires both operator grid and target lat/lon
    my_lat, my_lon = grid_to_latlon(my_grid)
    distance_km: int | None = None
    if lat is not None and lon is not None and my_lat is not None and my_lon is not None:
        distance_km = haversine_km(my_lat, my_lon, lat, lon)

    # Social fields from HamQTH (non-empty values only)
    hq_data = (results.get("hamqth") or {}).get("data") or {}
    social = {k: v for k, v in {
        "twitter":  hq_data.get("twitter"),
        "facebook": hq_data.get("facebook"),
        "linkedin": hq_data.get("linkedin"),
        "youtube":  hq_data.get("youtube"),
        "skype":    hq_data.get("skype"),
        "web":      hq_data.get("web"),
    }.items() if v}

    # Your own log (cached by logbooks.history); the lists travel outside results
    log_info      = results.get("log") or {}
    log_qsos      = log_info.pop("qsos", [])
    top_contacts  = log_info.pop("top_contacts", [])
    ecr_coasters  = get_ecr_coasters()

    return jsonify({
        "callsign":          callsign,
        "results":           results,
        "social":            social,
        "version":           APP_VERSION,
        "ecr_coaster":       callsign.upper() in ecr_coasters,
        "log_qsos":          log_qsos,
        "top_contacts":      top_contacts,
        "callsign_history":          callsign_history,
        "notable_entry":             notable_entry or None,
        "notable_is_current_holder": _notable_is_current,
        "current_holder_name":       _best_name,
        "location_blurb":            location_blurb,
        "portable_ops":              portable_ops,
        "solar":                     solar,
        "distance_km":               distance_km,
        "derived": {
            "lat":          lat,
            "lon":          lon,
            "grid":         grid,
            "grid_bounds":  get_grid_bounds(grid) if grid else None,
            "beam_heading": hdg,
            "beam_cardinal": card,
            "my_grid":      my_grid,
            "distance_km":  distance_km,
        },
    })


@app.route("/log_qso", methods=["POST"])
def log_qso():
    writers = logcfg.writers()
    if not writers:
        return jsonify({"status": "error", "results": [],
                        "error": "No logger configured for new QSOs (set LOG_WRITE)"}), 503

    body     = request.get_json(silent=True) or {}
    record, error = build_qso_record(body)
    if error:
        return jsonify({"status": "error", "results": [], "error": error}), 400

    results = logsources.write(writers, record)
    ok = [r for r in results if r["ok"]]
    for r in results:
        logger.info("log_qso %s -> %s: %s %s", record["call"], r["logger"],
                    "ok" if r["ok"] else "FAILED", r["message"])
    if ok:
        hist = _history()
        if hist is not None:
            hist.add(record)
    status = "ok" if len(ok) == len(results) else ("partial" if ok else "error")
    failed = "; ".join(f"{r['label']}: {r['message']}" for r in results if not r["ok"])
    return jsonify({"status": status, "results": results, "error": failed}), (200 if ok else 502)


_PHONE_MODES = {"SSB", "USB", "LSB", "FM", "AM", "DV", "PHONE"}


def build_qso_record(body: dict) -> tuple[dict, str]:
    """The QSO from the Log QSO form as an ADIF-style record (lower-case field names)."""
    def val(key: str) -> str:
        return str(body.get(key) or "").strip()

    callsign = val("callsign").upper()
    band     = val("band")
    mode     = val("mode").upper()
    dt_on    = val("datetime_on")  # "YYYYMMDD HHMMSS"

    if not callsign or not band or not mode or not dt_on:
        return {}, "callsign, band, mode, and datetime_on are required"
    parts = dt_on.split()
    if len(parts) != 2 or len(parts[0]) != 8 or len(parts[1]) != 6 or not "".join(parts).isdigit():
        return {}, "datetime_on must be 'YYYYMMDD HHMMSS'"
    qso_date, time_on = parts

    # 59 for phone modes, 599 for CW and digital
    default_rst = "59" if mode in _PHONE_MODES else "599"
    record = {
        "call":     callsign,
        "band":     band,
        "mode":     mode,
        "qso_date": qso_date,
        "time_on":  time_on,
        "time_off": time_on,
        "rst_sent": val("rst_sent") or default_rst,
        "rst_rcvd": val("rst_rcvd") or default_rst,
    }
    for key, upper in [("freq", False), ("submode", True), ("tx_pwr", False), ("comment", False),
                       ("notes", False), ("name", False), ("gridsquare", True), ("country", False),
                       ("state", False), ("cnty", False), ("qth", False), ("cont", True),
                       ("dxcc", False), ("cqz", False), ("ituz", False), ("iota", True),
                       ("lat", False), ("lon", False), ("distance", False)]:
        v = val(key)
        if v:
            record[key] = v.upper() if upper else v
    return record, ""


if __name__ == "__main__":
    # The Werkzeug debugger runs code from the browser: only with FLASK_DEBUG=1, and only on localhost
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", port=5000)
