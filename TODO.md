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
- A speaker `media_player` (`play_media` and announce, so TTS works), sending audio over the camera's RTSP
  audio backchannel. The agent API has no playback action, so this means writing an RTSP client in the
  integration. Roughly 500 lines plus 250 of tests; the risk is camera-side behaviour, not the code.
  - **Raptor** (`rsd`, from its source):
    - It is offered in `DESCRIBE` only when the request carries `Require: www.onvif.org/ver20/backchannel`
      and `[rtsp] backchannel = true` (the `[audio] ao_enabled` speaker setting must be on too).
    - The offer is a `sendonly` audio track (`control:backchannel`) with PCMU, PCMA, Opus, AAC and L16
      (16 kHz, so no companding).
    - Flow: `DESCRIBE`, `SETUP` a track the camera sends (audio is cheap), `SETUP` the backchannel, `PLAY`,
      then RTP over TCP interleaved. `PLAY` is refused with 455 unless a sending track was set up.
    - The server stamps packets on arrival and ignores the RTP timestamp, so the client must pace 20 ms
      packets in real time.
    - RTSPS only on cameras built that way: TLS, Digest auth, port 322.
  - **prudynt** (from its source):
    - The backchannel is offered under the same `Require` tag, with `control:track0`, PCMU and PCMA (AAC and
      Opus only if built in). There is no L16.
    - It also accepts an older flow, `ANNOUNCE` then `SETUP track0` then `RECORD`, which needs no receive
      track.
    - It is enabled by `audio.spk_enabled` (default true).
    - RTSP is plain only (built with `NO_OPENSSL`), on port 554, with Digest (default) or Basic auth. TCP
      interleaved and UDP both work.
    - The client therefore has to handle both `rtsp` and `rtsps`, and both flows.
  - **Thinpiino** has no audio, so it gets no entity. Create the entity only when the camera reports the
    `audio.spk_enabled` capability, and report a clear error when the camera doesn't offer a backchannel.
  - **Audio conversion:** decode and resample to mono 16 kHz with PyAV (it ships with HA's `stream`
    integration), or the `ffmpeg` integration. Python 3.14 has no `audioop`, so µ-law is PyAV's `pcm_mulaw`
    or a small table.
  - **Checked on a Raptor camera (silent `DESCRIBE`, 2026-10-12):**
    - It offers the backchannel with PCMU, PCMA, Opus (48 kHz), AAC and L16 (16 kHz), plus an audio
      receive track (the mic was on).
    - RTSPS verifies against the public wildcard cert with default TLS settings, so no pinning is needed there.
    - Connect plus TLS handshake took 0.5 s and `DESCRIBE` 17 ms, so latency is not a worry.
    - Digest auth has no `qop` (RFC 2069 style), whereas prudynt uses `qop="auth"`. The client must handle both.
    - `OPTIONS` lists `DESCRIBE, SETUP, PLAY, PAUSE, TEARDOWN, GET_PARAMETER, SET_PARAMETER`, so this camera only
      supports the `Require` tag + `PLAY` flow, not `ANNOUNCE`/`RECORD`.
    - Playback works: a 1.5 s L16 tone at 10% amplitude was audible on the camera speaker almost immediately.
      `SETUP`, `SETUP` and `PLAY` took about 40 ms in total and the sender kept 20 ms pacing with no overrun.
      While playing, the camera sends its audio track back (about 12 KB/s), so keep sessions short.
  - **Still unknown:**
    - whether the camera offers an audio receive track when its mic is off;
    - whether a self-signed RTSPS cert (stock Thingino) can reuse the pinned agent cert;
    - audio quality over longer clips (dropouts at the speaker ring), which a short tone doesn't test.
- Recording controls and send2 toggles, for cameras that support them.
- Zeroconf discovery, once cameras (Thingino or Thinpiino) advertise themselves.
