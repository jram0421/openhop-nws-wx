# OpenHop NWS Weather

An [OpenHop](https://docs.openhop.dev/) Repeater plugin that relays National Weather Service alerts to a mesh channel and answers weather requests from other nodes.

> **A convenience relay, not a safety system.** It is not a replacement for NOAA Weather Radio or Wireless Emergency Alerts. Messages can be delayed, dropped or wrong. Do not rely on it to keep anyone safe.

## What it does

**Alert relay.** Polls `api.weather.gov/alerts/active` for your states, keeps alerts that cover your counties (SAME codes) or zones, and posts them to an alerts channel. It deduplicates by alert ID, filters by severity and event type, and enforces a cooldown and an hourly cap.

**Weather bot.** Listens on a bot channel for these commands (case does not matter):

| Command | Reply |
|---|---|
| `wx` | Forecast for the sender's node, from the GPS position in its last advert, plus a second message with any active alerts there |
| `wx <zip>` | Forecast for a US zip code, plus a second message with any active alerts there |
| `alerts` | Alerts currently active in your configured area (up to three) |
| `alerts <zip>` | Every active alert at that zip code, most severe first |
| `help` or `?` | The command list and a short disclaimer |

Replies mention the sender as `@[name]`. Each sender is limited to one command every 20 seconds and 12 per hour; over the limit the bot stays silent.

## Requirements

- An OpenHop Repeater with the plugin manager.
- A Companion frame server the plugin can reach, with two channels created on that Companion: a bot channel (default `WX`) and an alerts channel (default `WX-Alerts`). Names are matched ignoring case and a leading `#`.
- Python 3.11+ on the Repeater host. The plugin gets its own virtualenv.
- An email address, sent to NWS in the `User-Agent` header as they ask of API users.

## Install

1. Download the wheel from the [releases page](../../releases), or build it (see Development).
2. Upload it on the Repeater dashboard's plugin page and enable it.
3. Open `http://<repeater>:8000/plugins/nws-wx/` while logged in to the dashboard.
4. Enter your email, set the frame server host and port (default `127.0.0.1:5000`), pick states and counties, then **Save and restart**.
5. Click **Save and send test alert** and confirm the message arrives on the alerts channel.

## Configuration

The config page edits the plugin's `config.json`. Fields:

| Field | Default | Notes |
|---|---|---|
| `contact_email` | none | Required. Used in the NWS `User-Agent`. The plugin does not contact NWS until it is valid |
| `frame_host`, `frame_port` | `127.0.0.1`, `5000` | The Companion frame server |
| `wx_channel`, `alerts_channel` | `WX`, `WX-Alerts` | Channel names on the Companion |
| `bot_enabled`, `alerts_enabled` | `true` | Turn either feature off |
| `wx_alerts_followup` | `true` | Send the second, alerts message after a `wx` forecast |
| `states` | none | Two-letter codes. Required when alerts are on |
| `counties` | none | SAME codes such as `048113`. Empty means the whole state |
| `zones` | none | Zone codes such as `TXZ104`, for alerts issued by zone |
| `min_severity` | `Moderate` | `Minor`, `Moderate`, `Severe` or `Extreme` |
| `events` | none | Event names to allow. Empty means all |
| `poll_seconds` | `120` | 60 to 3600 |
| `alert_cooldown_seconds` | `60` | Minimum gap between posted alerts |
| `alert_hourly_cap` | `10` | Most alerts posted per hour |

**How the page saves.** Plugin pages are not given the dashboard login by the Repeater, so the page reads the dashboard's own session token from the browser (`pymc_jwt_token` in `localStorage`, same origin) and uses it for the settings API. If you are logged out or the session expired, it says so and shows the JSON to paste into the dashboard's plugin Config dialog instead. The settings API replaces the whole config object, and the page keeps keys it does not know about.

**Status panel.** The top of the page shows the plugin state, frame server connection and channels, whether NWS is answering (a check about every five minutes, plus every alert poll), the last poll, and warnings. The plugin also writes this to `runtime.json`.

## Behavior to know about

- **One client per frame server.** The Companion frame server allows a single client and a new connection evicts the old one. If another app auto-reconnects to the same port, the two will keep evicting each other. Give the bot its own frame server port if you can.
- **The bot reads the Companion's message queue.** While connected, it pulls pending messages, including direct messages, out of the queue, so another app on the same Companion may not see them.
- **Message length.** Channel text shares 160 bytes with the sending node's name. The plugin reads its node name on connect and fits replies to what is left, showing whole forecast periods only.
- **Sender location.** `wx` finds the sender by the name in the channel message and looks it up in the Companion's contacts. It will not guess between nodes with the same name, and it uses the position from the node's last advert, not a live one. A node reporting 0,0 is treated as having no GPS.
- **Privacy.** Zip codes are resolved from a table bundled in the package (Census ZCTA centroids), so they are never sent to a third party. NWS receives the coordinates of the forecast being requested and your contact email.
- **Alerts and restarts.** Alert IDs already posted are saved in `state.json`, so restarts do not repeat them.

## Development

```
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest        # tests use mocked responses, no network
.venv/bin/pip install build && .venv/bin/python -m build --wheel
```

Layout: `src/nws_wx/` holds the plugin (`frames.py` is the frame client, `alerts.py` the poller and relay, `weather.py` forecasts, `bot.py` commands, `main.py` the supervisor), `ui/` is the config page, and `PROTOCOL.md` records the frame protocol as verified against `openhop_core`.

Bundled data is generated, not hand-edited:

- `scripts/build_ui_data.py` makes `ui/data.js` (states, counties, event types) from the Census county list and `https://api.weather.gov/alerts/types`.
- `scripts/build_zip_data.py` makes `src/nws_wx/zipdata.json` from the Census 2020 ZCTA gazetteer.

Each script's docstring has the download URLs. When you change the version, update it in `pyproject.toml`, `openhop-plugin.json` and `src/nws_wx/__init__.py`, which must all match.

## Data sources

Weather data comes from the [National Weather Service API](https://www.weather.gov/documentation/services-web-api). County and zip data come from US Census Bureau files, which are public domain.
