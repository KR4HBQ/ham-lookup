# Ham Lookup

A self-hosted web app that looks up an amateur radio callsign across many sources at once
(FCC ULS, Callook, QRZ.com, HamQTH, QRZCQ, Club Log, PSK Reporter, AllStarLink, POTA/SOTA),
shows the station on a map with beam heading and distance from your grid, and checks your own
log for previous QSOs. You can log a new QSO straight from the result.

Your log can come from Wavelog, QRZ Logbook, Club Log, LoTW, World Radio League or any ADIF file,
and new QSOs can go to Wavelog, Cloudlog, QRZ Logbook, Club Log and World Radio League, several at
once. See [Your log](#your-log).

Every user runs their own copy with their own API keys. Nothing is shared or hosted for you.

## Quick start (Docker)

```bash
git clone https://github.com/KR4HBQ/ham-lookup.git
cd ham-lookup
cp .env.example .env      # then edit .env with your own keys and grid
docker compose up -d --build
```

Open http://localhost:5000 (or your server's address on port 5000).

To update later:

```bash
git pull && docker compose up -d --build
```

`rebuild.sh` does the same thing with plain `docker` commands, for hosts without Compose
(for example Unraid's terminal).

Your log is cached in `./data` (mounted into the container), so it survives rebuilds. That folder
holds your QSOs and is ignored by git.

## Without Docker

Requires Python 3.10 or newer.

```bash
pip install -r requirements.txt
cp .env.example .env      # edit it
python app.py
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The tests are offline: every logger API is replaced by a fake, and your `.env` is never read.

## Keep your keys private

`.env` holds your passwords and API keys. It is listed in `.gitignore`; never commit it or paste it
into an issue. Club Log in particular scans GitHub and revokes any application key it finds there.

## Security and privacy

- **Login is optional.** Without one, anyone who can open the page can look up calls and log QSOs to
  your loggers. On a home network that is usually fine; don't forward port 5000 from your router.
  Set `HAMLOOKUP_PASSWORD` in `.env` and the browser asks for that password first (any user name
  works). If you want it reachable from outside, put it behind HTTPS too: a VPN (WireGuard,
  Tailscale), Cloudflare Access, or a reverse proxy. The password travels with every request, so
  over plain HTTP anyone on the path could read it.
- **Error messages hide keys.** Several services take your password or API key in the request URL;
  Ham Lookup strips the query string from every error it shows or logs.
- **`data/` holds a copy of your log** (callsigns, dates, names). It is ignored by git and Docker builds.
  Delete it any time; it is rebuilt from your logger.
- **Who sees what you look up.** Each lookup sends the callsign to the services you configured (QRZ,
  HamQTH, QRZCQ, Club Log, Callook, HamDB, PSK Reporter, AllStarLink, POTA/SOTA, FCC) and the station's
  location to Open-Meteo and Wikipedia. With `ANTHROPIC_API_KEY` set, the names of the callsign's
  current and previous license holders (public FCC data) are sent to Anthropic to check notability.
- **`python app.py` runs without the Flask debugger.** Set `FLASK_DEBUG=1` only on your own machine:
  the debugger can run code from the browser.

---

## Environment Variables

### Your Station
| Variable | Required | Description |
|---|---|---|
| MY_GRID | Recommended | Your Maidenhead grid square (e.g. FM05). Used to calculate beam heading to looked-up station. |
| CONTACT_INFO | Optional | Your email or website, added to the User-Agent sent to Wikipedia and PSK Reporter. |

### FCC ULS + Callook
No credentials needed. These sources are always active.

### QRZ.com XML API
| Variable | Required | Description |
|---|---|---|
| QRZ_USER | Yes (for QRZ) | Your QRZ.com username |
| QRZ_PASS | Yes (for QRZ) | Your QRZ.com password |

Note: Full data (bio, photo, exact address) requires a QRZ XML Data subscription (~$25/yr).
Basic license data returns without subscription but photo/bio will be absent.
Get subscription: https://www.qrz.com/page/xml_data.html

### HamQTH (free)
| Variable | Required | Description |
|---|---|---|
| HAMQTH_USER | Yes (for HamQTH) | Your HamQTH.com username |
| HAMQTH_PASS | Yes (for HamQTH) | Your HamQTH.com password |

Free account: https://www.hamqth.com/register.php
No subscription required — full API access at no cost.

### QRZCQ (free)
| Variable | Required | Description |
|---|---|---|
| QRZCQ_USER | Yes (for QRZCQ) | Your QRZCQ.com username |
| QRZCQ_PASS | Yes (for QRZCQ) | Your QRZCQ.com password |

Free account: https://www.qrzcq.com

### Clublog (free application key)
| Variable | Required | Description |
|---|---|---|
| CLUBLOG_API_KEY | Yes (for Clublog) | 40-character hex application API key (also used to log QSOs to Club Log) |

Request your key: https://clublog.org/requestapikey.php
This is a per-application key, not per-user. Keep it out of public repos — Clublog scans GitHub and auto-revokes exposed keys.
Provides: DXCC entity, CQ zone, continent, lat/lon, blocked status.

## Your log

Two settings decide where your log comes from and where new QSOs go:

| Variable | Description |
|---|---|
| LOG_SOURCE | Where "worked before" history comes from: `wavelog`, `qrz`, `clublog`, `lotw`, `wrl` or `adif`. Unset: Wavelog, when `WAVELOG_URL` and `WAVELOG_KEY` are set. |
| LOG_WRITE | Loggers that new QSOs go to, comma separated: `wavelog`, `cloudlog`, `qrz`, `clublog`, `wrl`, e.g. `wavelog,qrz`. Unset: Wavelog, when it is configured. Empty: no Log QSO button. |
| LOG_REFRESH_MINUTES | Optional. How often the cached log is refreshed. Default per source, below. |
| DATA_DIR | Optional. Where the cache is kept. Default `data` in the app folder. |

If you used Ham Lookup with Wavelog before these settings existed, nothing changes: leave both unset.

| Logger | Read (worked before) | Write (Log QSO) | Refreshed every |
|---|---|---|---|
| Wavelog | Yes | Yes | 5 min |
| Cloudlog | No export API: export ADIF and use `adif` | Yes | |
| QRZ Logbook | Yes (needs a QRZ subscription) | Yes | 5 min new QSOs, whole log every 6 h |
| Club Log | Yes | Yes (real-time upload) | 60 min |
| LoTW | Yes | No: LoTW only accepts TQSL-signed uploads | 6 h (a full report can take minutes) |
| World Radio League | Yes (no QSL status) | Yes (needs a frequency) | 60 min |
| ADIF file or URL | Yes (any logger that exports ADIF) | No | 5 min |

The log check, the QSO list and Your Top Contacts all come from this cache, so a lookup never waits
on your logger. A QSO you log from Ham Lookup appears in the log check at once. When you log to several
loggers, each one reports separately; if one fails, the others still get the QSO. A logger that
already has the QSO counts as logged.

### Wavelog
| Variable | Required | Description |
|---|---|---|
| WAVELOG_URL | Yes | Base URL of your Wavelog instance, e.g. http://192.168.1.x or https://yourdomain.com |
| WAVELOG_KEY | Yes | API key (Wavelog → Settings → API). Read+write to log QSOs. |
| WAVELOG_STATION_ID | Optional | Station Location ID that is read and logged to (Wavelog → Station Locations). Default 1. |

### Cloudlog
| Variable | Description |
|---|---|
| CLOUDLOG_URL | Base URL of your Cloudlog instance |
| CLOUDLOG_KEY | A read+write API key, made on Cloudlog's API page |
| CLOUDLOG_STATION_ID | Station Location ID to log to |

Cloudlog has no API to export a log, so for worked-before history export ADIF from Cloudlog
(or publish it at a URL) and set `LOG_SOURCE=adif`.

### QRZ Logbook
| Variable | Description |
|---|---|
| QRZ_LOGBOOK_KEY | The logbook's API key (QRZ Logbook → Settings). Separate from `QRZ_USER`/`QRZ_PASS`, which are for callsign data. |

The Logbook API needs a QRZ subscription.

### Club Log
| Variable | Used for | Description |
|---|---|---|
| CLUBLOG_TOKEN | Read | REST token from Club Log → Settings → API (starts with `cl_`) |
| CLUBLOG_CALLSIGN | Read, write | The log's callsign |
| CLUBLOG_EMAIL | Write | Your Club Log email |
| CLUBLOG_APP_PASSWORD | Write | An Application Password (Club Log → Settings → App Passwords), not your login password |
| CLUBLOG_API_KEY | Write | Your application API key, the same one the DXCC lookup uses |

**The Club Log API key is yours alone.** Club Log issues it per application and revokes any key it
finds on GitHub or elsewhere in public. Request your own at https://clublog.org/requestapikey.php,
keep it in `.env`, and never commit it. Club Log also firewalls clients that keep sending a bad login,
so after one failed login Ham Lookup stops writing to Club Log until it is restarted.

### LoTW
| Variable | Description |
|---|---|
| LOTW_USERNAME | Your LoTW website login |
| LOTW_PASSWORD | Your LoTW website password |
| LOTW_OWNCALL | Optional: only QSOs made as this callsign |

Read only. LoTW builds the whole report on every request, so it is read every 6 hours.

### World Radio League
| Variable | Description |
|---|---|
| WRL_API_KEY | WRL → Integrations → Developer API (`wrl_live_...`) |
| WRL_LOGBOOK_ID | Optional: logbook to log to (default: your default WRL logbook) |
| WRL_STATION_CALLSIGN | Optional: your station callsign (default: the logbook's or your profile's) |

WRL contacts carry no QSL status, so the log check shows no confirmations. Logging to WRL needs the
frequency filled in.

### ADIF file or URL
| Variable | Description |
|---|---|
| ADIF_PATH | Path to an `.adi` file (relative to the app folder; in Docker put it in `./data`), or an http(s) URL |

If you have never worked the looked-up callsign, the log check shows "Not in Log". That is expected.
No LoTW/eQSL data is shown for unworked stations, as neither service has a public lookup API.

---

## Source Capabilities Matrix

| Source | License Data | Photo | Bio | Grid | LoTW flag | eQSL flag | Cost |
|---|---|---|---|---|---|---|---|
| FCC ULS | ✓ (US only) | ✗ | ✗ | ✗ | ✗ | ✗ | Free |
| Callook | ✓ (US only) | ✗ | ✗ | ✓ | ✗ | ✗ | Free |
| QRZ XML | ✓ | ✓* | ✓* | ✓ | ✓ | ✓ | Free account / $25/yr for full data |
| HamQTH | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | Free |
| QRZCQ | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ | Free |
| Clublog | DXCC only | ✗ | ✗ | ✓ | ✗ | ✗ | Free (app key) |
| Your log | Your log only | ✗ | ✗ | ✗ | Per-QSO | Per-QSO | Depends on the logger |
| PSK Reporter | Activity only | ✗ | ✗ | ✓ | ✗ | ✗ | Free |
| Open-Meteo | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | Free |

*QRZ full bio/photo requires XML subscription

### Other optional settings
| Variable | Description |
|---|---|
| HAMLOOKUP_PASSWORD | Ask for this password before showing Ham Lookup. Blank: no password. |
| MAPTILER_API_KEY | Interactive maps. Free key at https://cloud.maptiler.com/account/keys/ |
| ANTHROPIC_API_KEY | Enables the notability check on a callsign's previous holders. |

## License

MIT, see [LICENSE](LICENSE).
