# ADR 0004 — Expose the Archive Server over Tailscale Serve

## Status
Accepted

## Context
The Archive Server (ADR 0001) binds `0.0.0.0:8080` with no authentication and was
designed for a trusted home LAN, reached via mDNS (`pop-os.local:8080`). Reading on
the go worked only by running a Tailscale client on the laptop and reaching the
desktop over the tailnet by IP — ad hoc, per-device, and without HTTPS.

The goal is off-LAN access to digests and the Library from the reader's own devices,
with real HTTPS and without exposing the server — which has unauthenticated *write*
endpoints (`/api/rate`, `/api/star`, `/api/tag`, `/api/mark-read`) — to the public
internet.

## Decision
Install Tailscale on the host and expose the server with Tailscale Serve:

```
sudo tailscale serve --bg 8080
```

This proxies HTTPS (443) on the node to `http://127.0.0.1:8080` and publishes it at
`https://<host>.<tailnet>.ts.net/`, reachable **only by devices signed into the
tailnet**. Tailnet membership is the authentication, which is what makes it safe to
leave the server's own write endpoints unauthenticated. `--bg` persists the config
in tailscaled node state, so it is restored automatically on reboot.

Public exposure via Tailscale Funnel (`tailscale funnel --bg 8080`) is deliberately
*not* enabled: it would make the write endpoints world-reachable, and is deferred
until an authentication layer is added.

## Consequences
- Digests and the Library are reachable over real HTTPS from any tailnet device,
  anywhere — no longer limited to the home LAN or a hand-configured client.
- Availability depends on the desktop being powered on and online; this is not a
  hosted service (see the rejected VPS/managed-storage options in prior discussion).
- The server still binds `0.0.0.0:8080`, so it remains reachable on the LAN as well.
  Binding `127.0.0.1:8080` would make Tailscale the only ingress; deferred as an
  optional hardening.
- Switching to public access is a one-command change (`funnel` in place of `serve`)
  but must be paired with adding authentication first.
