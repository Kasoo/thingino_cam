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
| `rtsp.py` | Builds the stream URL from the agent's `rtsp_url` (`stream_target`, shared by the camera and the speaker). |
| `camera.py` | Stream source and cached snapshots. |
| `backchannel.py` | RTSP(S) client that sends audio over the camera's backchannel. No HA imports. |
| `audio.py` | Decodes any media to 16 and 8 kHz mono PCM with PyAV. No HA imports. |
| `media_player.py` | The speaker: fetch, decode, play, replace or stop a clip. |
| `binary_sensor.py`, `switch.py`, `select.py`, `number.py`, `sensor.py` | One module per platform; `entity.py` is the shared base. |
| `strings.json`, `translations/en.json` | UI text. Keep the two identical. |
| `brand/` | Icon only, rendered from `brand-src/` (repo root): `icon.svg` (original, hand-drawn lettering; don't replace it with Thingino's logo) and `render.sh` (needs inkscape). No logo on purpose: a wordmark looked like Thingino's own, and without one HA shows the icon in its place. |
| `images/` | README screenshots (not part of the package). `device.png` is the device page with its cards in three columns. `compose_device.py` makes it from a full-page dark-theme screenshot (`python3 -I images/compose_device.py SHOT.png images/device.png --layout 'a c / b e / d'`; the card letters are explained in its docstring; `--blur` hides parts). The README links the image by its raw GitHub URL, because HACS doesn't resolve relative paths. |

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
  - At startup: `/state`, `/config` and the settings `/config` lacks (`audio/mic-enabled`,
    `motion/sensitivity`, `motion/cooldown-time`), **one at a time**.
  - Then every 15 min: `/config` (catches web UI edits), plus `/health`, one at a time.
  - `/health` is only polled while an enabled entity needs it (`uses_health` on the entity: last boot,
    load average, IP address, which are all disabled by default). Each such entity registers in
    `coordinator.health_users` when added to HA, so disabled ones never count. Until `/health` has been
    read, `CameraData.system`/`network` fall back to the startup `/state`, which carries the same blocks.
- **Events.** A background task holds `GET /events` (SSE) and reconnects every 15 s.
  - `motion.started`/`motion.stopped` drive the motion sensor directly.
  - `sensor.sample` feeds the day/night state.
  - `state.changed` (unless it only lists `motion.active`), `streamer.restarted` and `health.warning`
    trigger a `/health` re-read, again only while a `uses_health` entity is enabled.
- **Writes:** `PATCH /settings/<path>` with `{field: value}` (URLs use dashes, JSON underscores), mirrored
  into the cache without a re-read. Day/night: `POST /actions/daynight {"mode": ...}`.
- **Video.** The URL comes from `/state` `streams[0].rtsp_url`, but only its scheme, port and path are
  used; the host is always the configured one. Upstream Raptor omits brackets around IPv6 addresses and
  prudynt reports `127.0.0.1`, so keep tolerating both. Agents without `rtsp_url` get RTSPS on the
  configured port. The RTSP login is entered at setup, because the agent doesn't report it.
- **Snapshots:** `POST /actions/snapshot?stream_id=0|1` (1 for small thumbnails), cached 60 s per stream,
  concurrent requests share one fetch, one retry, then the last good still. With privacy on, the camera
  entity reports off.
- **Capabilities.** Entities are created from `/capabilities`; anything a camera reports as unsupported
  is left out.
- **Speaker** (`media_player`). The agent has no playback action, so audio goes over the RTSP audio
  backchannel (ONVIF Profile T), on one connection with RTP interleaved.
  - Flow: `DESCRIBE` with `Require: www.onvif.org/ver20/backchannel` (without the tag the offer is left out),
    `SETUP` an audio track the camera sends, `SETUP` the backchannel, `PLAY`. Raptor refuses `PLAY` with 455
    unless a track it sends is set up; prudynt doesn't need one. Video is never set up, so a Raptor camera
    that offers no audio track can't play, and the error suggests turning the mic on (whether Raptor drops
    the track when the mic is off is unverified). After a refusal, the client tries prudynt's
    older `ANNOUNCE`/`RECORD` flow on a new connection. `OPTIONS` `Public` can't choose between them:
    neither streamer lists `ANNOUNCE`.
  - Codec: L16 at 16 kHz if offered (Raptor), else PCMU at 8 kHz (prudynt). `audio.py` makes both rates in
    one decoding pass.
  - Sending is paced at one 20 ms packet per 20 ms: Raptor stamps packets on arrival and ignores the RTP
    timestamp, so a burst would garble the audio. The session timeout is 60 s, so a `GET_PARAMETER`
    keepalive goes every 30 s. Clips are capped at 120 s and downloads at 10 MB.
  - The call returns when the clip has played. A new clip cancels the one playing (which tears down
    first), so only one session runs at a time. `TEARDOWN` always runs, also on errors and cancellation.
  - A speaker turned off on the camera (`audio/spk-enabled`, read at play time) is an error, not switched
    on. RTSPS trusts the system store, then the pinned agent cert if there is one. Whether stock Thingino's
    RTSPS listener uses the agent's cert is unverified.
  - Created only when `/capabilities` reports `audio.spk_enabled`. On the camera, the backchannel needs
    `[rtsp] backchannel = true` and `[audio] ao_enabled` on Raptor (RTSPS on 322 on cameras built that
    way), and `audio.spk_enabled` on prudynt (plain RTSP on 554).
- **Entities left out on purpose:** reboot and restart-streamer buttons, speaker switch, scene brightness,
  memory and a streamer-health sensor (not what people want in HA).
- **Entities disabled by default:** image tuning (brightness, contrast, saturation, sharpness), the
  privacy mode and flip switches, and the diagnostics (last boot, load average, IP address, event
  stream). Load average is the 1-minute `/proc/loadavg`, which counts uninterruptible-sleep tasks and so
  reads 4-5 on a mostly idle T20; the agent reports no CPU percentage.
- Repeat connection failures are logged at debug level only.

## API reference

`docs/thingino/next/api-rfc.md` and `camera-agent.md` in thingino-firmware, and `agentctl schema` on a
camera. Field names come from the adapters: `package/thingino-raptor/files/agent-adapter` and
`package/prudynt-t/files/agent-adapter`.

## Testing

- `pytest` in a venv: `python3.14 -m venv .venv && .venv/bin/pip install -r requirements_test.txt`, then
  `.venv/bin/python -m pytest`. Covered: `backchannel.py` (against `tests/fake_rtsp.py`, a fake RTSP server
  shaped like Raptor or prudynt), the speaker entity, `rtsp.py`, the config flow and entry migration. Not yet:
  the SSE parser and the coordinator (`TODO.md`).
- Tested on a Petcube Play 1 (Ingenic T20, Raptor streamer, RTSPS) in both modes, and on Thinpiino (Pi Zero
  W), with Home Assistant 2026.9. The speaker has played TTS on the Petcube (L16 over RTSPS).
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
