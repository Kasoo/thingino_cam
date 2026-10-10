# AGENTS.md: thingino_cam

Notes for working on this repo. User-facing docs are in `README.md`; keep that short and put the detail here.

## What it is

A Home Assistant custom integration (domain `thingino_cam`) for cameras running Thingino. It uses
Thingino's camera agent API (`agentd`, `/api/v1`), which upstream designed as *the* API for remote
clients (`docs/thingino/next/camera-agent.md` in thingino-firmware: "optional HTTPS listener for remote hub
access"; the web UI is meant to be one of its clients). One config entry is one camera is one HA device.

It is not part of Thingino. Don't present it as official. Thinpiino (a Raspberry Pi agent) implements
the subset of the API this integration uses, and is the second test target.

## Files

Repo layout is the one HACS expects: one integration, all of it in `custom_components/thingino_cam/`
(including `brand/icon.png`, where HACS's brands check looks), `hacs.json` at the root.

| File | What |
|---|---|
| `api.py` | aiohttp client: `Connection`/`build_connection` for both modes, TLS contexts and pinning, the SSE parser, error mapping. |
| `config_flow.py` | Setup (`user` → `direct`/`webui` → `confirm_cert` → `rtsp`) and reauth. |
| `coordinator.py` | Startup reads, the safety-net poll, the event loop. |
| `rtsp.py` | Builds the stream URL from the agent's `rtsp_url`. |
| `camera.py` | Stream source and cached snapshots. |
| `binary_sensor.py`, `switch.py`, `select.py`, `number.py`, `sensor.py`, `button.py` | One module per platform; `entity.py` is the shared base. |
| `strings.json`, `translations/en.json` | UI text. Keep the two identical. |
| `brand/` | Icon and logo, rendered from `brand-src/` (repo root): `icon.svg` and `logo.svg` (original, hand-drawn lettering; don't replace them with Thingino's logo) and `render.sh` (needs inkscape). |

## Design

- **Two modes**, chosen at setup and stored as `mode` in the entry (config entries are version 2; v1
  entries migrate to `mode: direct`):

  | Mode | URL | Auth |
  |---|---|---|
  | `direct` (default) | `https://<host>:1998/api/v1` | `Authorization: Bearer <agent.token>`, optional mTLS client cert |
  | `webui` | `https://<host>/x/agent.cgi/api/v1` | `X-API-Key: <web UI API key>` |

  Direct is preferred: mutual TLS is upstream `security.md`'s preferred trust model, and the web UI API
  key can do anything on the camera. Web UI mode exists because stock firmware ships the agent
  local-only and exposing it needs a shell.
- **Web UI mode needs two Thingino fixes** not yet released: uhttpd passing `X-API-Key` to CGI, and
  `agent.cgi` forwarding the event-stream path. `MIN_WEBUI_BUILD_DATE` in `const.py` is checked against
  the build date the camera reports (Thingino has no version numbers). Setup also checks that the key is
  accepted in the header and that a `hello` event arrives within `EVENT_PROBE_SECONDS`; those probes are
  the real gate until the minimum date can be bumped.
- **mTLS** (`agent.client_ca`, `agent.client_cn` on the camera) is not in stock Thingino yet. Thinpiino
  supports it. The client cert and key are file paths, relative to the HA config directory.
- **Certificates.**
  - A publicly trusted cert needs nothing.
  - Anything else (Thingino's self-signed cert) is shown by SHA-256 fingerprint and pinned when
    confirmed, stored as `server_cert`. HA then trusts exactly that cert, whatever host name it carries.
  - A public cert reached by another name (e.g. an IP address) is refused, not pinned: pinning a Let's
    Encrypt cert would break at renewal.
  - Reauth re-checks the server cert.
- **Reads are event-driven; polling is a 15-minute safety net.** Every agent call runs forking shell
  adapters on a single-core MIPS SoC. Measured on a T20: `/state` 2-10 s, `/config` ~4.5 s, `/health`
  ~1 s, a single setting 0.2-0.3 s. Polling six endpoints every 60 s drove the camera to 0% idle.
  - At startup: `/state`, `/config`, the settings `/config` lacks (`audio/mic-enabled`, `audio/spk-enabled`,
    `motion/sensitivity`, `motion/cooldown-time`) and `/health`, **one at a time**.
  - Then every 15 min: `/health` and `/config` (catches web UI edits), one at a time.
- **Events.** A background task holds `GET /events` (SSE) and reconnects every 15 s.
  - `motion.started`/`motion.stopped` drive the motion sensor directly.
  - `sensor.sample` feeds scene brightness and the day/night state.
  - `state.changed` (unless it only lists `motion.active`), `streamer.restarted` and `health.warning`
    trigger a `/health` re-read.
- **Writes:** `PATCH /settings/<path>` with `{field: value}` (URLs use dashes, JSON underscores), mirrored
  into the cache without a re-read. Day/night: `POST /actions/daynight {"mode": ...}`. Buttons:
  `POST /actions/reboot`, `/actions/streamer/restart`.
- **Video.** The URL comes from `/state` `streams[0].rtsp_url`, but only its scheme, port and path are
  used; the host is always the configured one. Upstream Raptor omits brackets around IPv6 addresses and
  prudynt reports `127.0.0.1`, so keep tolerating both. Agents without `rtsp_url` get RTSPS on the
  configured port. The RTSP login is entered at setup, because the agent doesn't report it.
- **Snapshots:** `POST /actions/snapshot?stream_id=0|1` (1 for small thumbnails), cached 60 s per stream,
  concurrent requests share one fetch, one retry, then the last good still. With privacy on, the camera
  entity reports off.
- **Capabilities.** Entities are created from `/capabilities`; anything a camera reports as unsupported
  is left out.
- Repeat connection failures are logged at debug level only.

## API reference

`docs/thingino/next/api-rfc.md` and `camera-agent.md` in thingino-firmware, and `agentctl schema` on a
camera. Field names come from the adapters: `package/thingino-raptor/files/agent-adapter` and
`package/prudynt-t/files/agent-adapter`.

## Testing

- No automated tests yet (`TODO.md`).
- Tested on a Petcube Play 1 (Ingenic T20, Raptor streamer, RTSPS) in both modes, and on Thinpiino (Pi Zero
  W), with Home Assistant 2026.9.
- hassfest, as CI runs it (`.github/workflows/validate.yml`, which also runs the HACS check):
  `podman run --rm -v "$PWD:/github/workspace:ro,Z" ghcr.io/home-assistant/hassfest` from the repo root.
  UI strings must not contain URLs (use description placeholders).
- To try a change, copy `custom_components/thingino_cam` into a test HA instance's `custom_components/`
  and restart HA. `python3 -m py_compile custom_components/thingino_cam/*.py` catches syntax errors.

## Conventions

- No `requirements` in the manifest: only what Home Assistant already ships (aiohttp, voluptuous).
- Keep `README.md` short: what it does, how to install it, how to add a camera.
- Outstanding work goes in `TODO.md`. Remove an item when it's done; don't leave TODOs scattered in code.
- Releases are GitHub releases (HACS ignores bare tags). The tag matches `version` in `manifest.json`;
  bump it for every release.
- Nothing about any particular network or deployment belongs here.
