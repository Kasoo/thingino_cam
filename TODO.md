# TODO

## Before publishing

What HACS needs (hacs.xyz "Publish" and "Include" pages, and the checks in `hacs/action`):

- Publish a GitHub release (a tag alone isn't enough). Make the tag match `version` in `manifest.json`.
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
- An `update` entity (`/actions/firmware/install`, progress on `firmware.progress` events). Upstream's
  `api-rfc.md` defers the endpoint until the hub/camera trust model is done. Thingino has no version
  numbers (only a build date), so it also needs a source for the "latest" version.

## Features

- A voice satellite (`assist_satellite`): the camera's microphone feeds Assist and replies play on its speaker.
  Parked for now; the speaker half exists. It needs:
  - the receive half of the RTSP client, i.e. reading the camera's AAC audio (RFC 3640 packets) and decoding
    it to 16 kHz PCM; Raptor's HTTP `/audio` endpoint (WAV/ADTS) may be simpler where that daemon is enabled;
  - a satellite entity with the pipeline plumbing, and a wake word (openWakeWord) on the HA side, or a
    button to start it (see below);
  - care for the camera's single core, since this holds an audio session open.
  There is no HA microphone platform, so the microphone switch stays a switch. Listening already works
  through the camera entity's stream (AAC audio is kept) while the mic is on.
- The camera's physical button, as an `event` entity (`pressed`, `released`, long press) and a way to start a
  voice pipeline (push to talk).
  - Thingino runs `thingino-button`, which reads an evdev `gpio-keys` device and runs commands per key
    (`/etc/thingino-button.conf`). Its default mapping is a danger for push to talk: a short tap speaks the
    IP address, holding 5 s runs `wlan reset; reboot`, and holding 20 s runs `firstboot -f` (factory reset).
    Any button feature has to remove or move those, which is a camera-side config change.
  - The agent API has no button event. Its event types are fixed (`motion.*`, `streamer.restarted`,
    `record.completed`, `firmware.progress`, `health.warning`, `state.changed`, `sensor.sample`), and the
    event stream is built per connection by the adapter, so a script cannot inject one. Options: ask upstream
    for `button.pressed`/`button.released` events (the proper fix), or use Thingino's MQTT discovery (it
    publishes a "doorbell button pressed" sensor) or an HA webhook called from a `thingino-button` mapping,
    neither of which goes through this integration.
  - Create the `event` entity only for cameras that report the capability once upstream has one.
- Recording controls and send2 toggles, for cameras that support them.
- Zeroconf discovery, once cameras (Thingino or Thinpiino) advertise themselves.
