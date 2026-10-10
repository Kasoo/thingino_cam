# TODO

## Before publishing

- Choose a licence and add `LICENSE`.
- Create the public repo and add it as a remote. Then in `manifest.json` point `documentation` at it (it
  still points at thingino-firmware), and add `issue_tracker` and `codeowners`.
- Ask the Thingino maintainers whether they're happy with the name "Thingino Camera" (if not, rename),
  and whether they'd rather take the integration upstream.
- Link Thinpiino from `README.md` once it has a public repo.
- Find the oldest Home Assistant release that works and put it in `hacs.json`. It says 2026.9.0, the
  only version tested. Icons in `brand/` need a recent release.
- Add GitHub Actions for hassfest and HACS validation.

## Testing

- Write tests with `pytest-homeassistant-custom-component`:
  - the config flow: both modes, certificate pinning, `cert_name_mismatch`, reauth, v1 to v2 migration;
  - `rtsp.py`: IPv4, bracketed and unbracketed IPv6, `127.0.0.1`, no `rtsp_url`;
  - the SSE parser and the coordinator's event handling.
- Test on more cameras. So far only a Petcube Play 1 (T20, Raptor, our mTLS build) and Thinpiino:
  - the prudynt streamer;
  - stock Thingino in direct mode (self-signed cert, token, no mTLS);
  - other SoCs, which may be quicker, so the 15-minute poll could be shorter.

## Waiting on Thingino

- Bump `MIN_WEBUI_BUILD_DATE` in `const.py` (and the README if needed) once both web UI fixes are in a
  release: uhttpd passing `X-API-Key` to CGI, and `agent.cgi` forwarding the event-stream path. Link the
  upstream PRs here once they're open.
- The agent doesn't report the RTSP login, so setup asks for it. If Thingino adds a way (upstream's
  `security.md` keeps media credentials separate, under a `media` scope), read it from there and keep the
  RTSP step as a fallback for older firmware.
- mTLS needs firmware with `agent.client_ca`/`agent.client_cn`, which isn't upstream yet. If it lands, say
  so in the README.

## Features

- An `update` entity (`/actions/firmware/install`).
- Recording controls and send2 toggles, for cameras that support them.
- Zeroconf discovery, once cameras (Thingino or Thinpiino) advertise themselves.
