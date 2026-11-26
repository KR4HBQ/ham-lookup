# Ham Lookup

A self-hosted web app that looks up an amateur radio callsign across many sources at once
(FCC ULS, Callook, QRZ.com, HamQTH, QRZCQ, Club Log, PSK Reporter, AllStarLink, POTA/SOTA),
shows the station on a map with beam heading and distance from your grid, and checks your own
Wavelog log for previous QSOs. You can log a new QSO to Wavelog straight from the result.

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

## Without Docker

Requires Python 3.10 or newer.

```bash
pip install -r requirements.txt
cp .env.example .env      # edit it
python app.py
```

## Keep your keys private

`.env` holds your passwords and API keys. It is listed in `.gitignore`; never commit it or paste it
into an issue. Club Log in particular scans GitHub and revokes any application key it finds there.

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
| CLUBLOG_API_KEY | Yes (for Clublog) | 40-character hex application API key |

Request your key: https://clublog.org/requestapikey.php
This is a per-application key, not per-user. Keep it out of public repos — Clublog scans GitHub and auto-revokes exposed keys.
Provides: DXCC entity, CQ zone, continent, lat/lon, blocked status.

### Wavelog
| Variable | Required | Description |
|---|---|---|
| WAVELOG_URL | Yes (for Wavelog) | Base URL of your Wavelog instance, e.g. http://192.168.1.x or https://yourdomain.com |
| WAVELOG_KEY | Yes (for Wavelog) | Your Wavelog API key (Settings → API Keys in Wavelog UI) |
| WAVELOG_SLUG | Optional | Station logbook public slug (Settings → Station Profiles → Public Slug) |
| WAVELOG_STATION_ID | Optional | Station Location ID that new QSOs are logged to (Wavelog → Station Locations). Default 1. |

The Wavelog block shows whether you have previously worked the looked-up callsign, with full QSO history including band, mode, RST, QSL/LoTW/eQSL confirmation status per contact, and any comments.
If the callsign has never been in your log, this block displays "Not in Log" — this is correct and expected behavior. No LoTW/eQSL data is available for unworked stations as neither service provides a public lookup API.

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
| Wavelog | Your log only | ✗ | ✗ | ✗ | Per-QSO | Per-QSO | Self-hosted |
| PSK Reporter | Activity only | ✗ | ✗ | ✓ | ✗ | ✗ | Free |
| Open-Meteo | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | Free |

*QRZ full bio/photo requires XML subscription

### Other optional settings
| Variable | Description |
|---|---|
| MAPTILER_API_KEY | Interactive maps. Free key at https://cloud.maptiler.com/account/keys/ |
| ANTHROPIC_API_KEY | Enables the notability check on a callsign's previous holders. |

## License

MIT, see [LICENSE](LICENSE).
