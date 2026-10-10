# Thingino Camera for Home Assistant

A Home Assistant integration for cameras running [Thingino](https://thingino.com) firmware. It talks
to the camera agent built into Thingino, and gives you one device per camera with:

- live video (RTSP or RTSPS) and snapshots;
- motion, pushed from the camera as it happens;
- privacy mode, microphone and speaker switches;
- image settings, flip, motion sensitivity;
- day/night mode;
- reboot and streamer restart buttons, and diagnostics.

Entities only appear for what the camera says it supports. It also works with
Thinpiino, which makes a Raspberry Pi look like a Thingino camera.

This is not part of the Thingino project.

## Install

With [HACS](https://hacs.xyz): add this repository as a custom repository (type **Integration**),
install **Thingino Camera**, and restart Home Assistant.

By hand: copy `custom_components/thingino_cam` into your Home Assistant `config/custom_components/`
and restart.

## Add a camera

Add the **Thingino Camera** integration and pick one of two ways to connect.

**Directly to the camera agent** (recommended). The agent only listens locally at first, so log in to
the camera over SSH once and run:

```sh
jct /etc/thingino.json set agent.listen "::"
jct /etc/thingino.json set agent.tls true
/etc/init.d/S95agent restart
jct /etc/thingino.json get agent.token
```

Enter the camera's address and the token that last command prints. Home Assistant then shows the
fingerprint of the camera's self-signed certificate. Confirm it, and only that certificate is trusted
from then on. A client certificate (mutual TLS) is optional, and only works with firmware that supports it.

**Through the web UI**, if you can't use SSH. Enter the API key from the camera's web UI (Web Interface
settings, API Access). This needs newer firmware than the direct mode; setup tells you if yours is too old.

Last, enter the video stream login. Thingino's default is `thingino` / `thingino`. The web UI shows
the real one under Streamer → Streamer config.

## Notes

- Motion and brightness arrive as events. Everything else is polled only every 15 minutes, because
  each request costs the camera noticeable CPU. Changes made in the camera's web UI can take that long
  to show.
- Snapshots are cached for 60 seconds, so dashboards don't load the camera.
- If the camera is reflashed and its key or certificate changes, Home Assistant asks you to
  re-authenticate.
