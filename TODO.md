# TODO

## Before publishing

What HACS needs (hacs.xyz "Publish" and "Include" pages, and the checks in `hacs/action`):

- Create the public GitHub repo (`Kasoo/thingino_cam`) and push.
- On GitHub, set a description and topics (e.g. `home-assistant`, `hacs`, `thingino`, `camera`) and keep
  issues enabled.
- Publish a GitHub release (a tag alone isn't enough). Make the tag match `version` in `manifest.json`.
- Check the validation workflow passes on GitHub. hassfest passes locally; the HACS check can only
  run once the repo exists, because it reads the repo's settings.
- To get into the HACS default list: after the above, open a PR on `hacs/default` adding the repo to
  `integration`, in alphabetical order. Do this from a personal account, not an organisation.
- Ask the Thingino maintainers whether they'd rather take the integration upstream.
- Link Thinpiino from `README.md` once it has a public repo.
- Find the oldest Home Assistant release that works and put it in `hacs.json`. It says 2026.9.0, the
  only version tested. Icons in `brand/` need a recent release.

## Testing

- Test on more cameras. So far only a Petcube Play 1 (T20, Raptor, our mTLS build) and Thinpiino:
  - the prudynt streamer, including the speaker: from its source, the ONVIF flow works without a receive
    track and `ANNOUNCE`/`RECORD` is the fallback, but neither has been tried on a camera;
  - stock Thingino in direct mode (self-signed cert, token, no mTLS);
  - the speaker on stock Thingino: whether its RTSPS listener uses the same self-signed cert as the agent
    (the speaker falls back to the pinned agent cert);
  - the speaker on Raptor with the mic off: whether the camera still offers an audio track to set up
    (without one, `PLAY` is refused with 455);
  - the speaker with clips near the 2-minute cap: dropouts, keepalive. Only short TTS has been played;
  - other SoCs, which may be quicker, so the 15-minute poll could be shorter.

## Waiting on Thingino

- Web UI mode (`/x/agent.cgi`, no SSH needed) is switched off until both fixes are in a Thingino release:
  uhttpd passing `X-API-Key` to CGI, and `agent.cgi` forwarding the event-stream path. Then set
  `MIN_WEBUI_BUILD_DATE` in `const.py` to that release, set `WEBUI_MODE_ENABLED`, and put the "through the
  web UI" paragraph back in the README (see git history). Link the upstream PRs here once they're open.
- The agent doesn't report the RTSP login, so setup asks for it. If Thingino adds a way (upstream's
  `security.md` keeps media credentials separate, under a `media` scope), read it from there and keep the
  RTSP step as a fallback for older firmware.
- mTLS needs firmware with `agent.client_ca`/`agent.client_cn`, which isn't upstream yet. If it lands, say
  so in the README.

## Features

- An `update` entity (`/actions/firmware/install`).
- Recording controls and send2 toggles, for cameras that support them.
- Zeroconf discovery, once cameras (Thingino or Thinpiino) advertise themselves.
