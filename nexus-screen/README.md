# Nexus Screen — Module 5, Phase 1 (Server Side)

The server half of Nexus's eyes and face. The Android app ("Nexus Screen",
in the goal workspace under `module-5-screen-control/android-app/`) watches
his phone's screen through the accessibility framework and streams tiny text
events here; this receiver turns them into one living piece of state per
device — *what's on his screen right now* — and pushes overlay updates and
the kill switch back down to the phone.

Design rules (from the approved blueprint): **event-driven only** (nothing
polls, nothing records), **no screenshots ever**, **in-memory state only**
(no history beyond the current screen), **loopback by default** (the phone
reaches the box over Tailscale, never the public internet), Python stdlib
only — it runs light on the 2-vCPU Nexus box.

## Layout

```
engine/awareness.py   the awareness engine: per-device state, coalescing,
                      meaningful-change detection, password redaction,
                      command queues, kill switch state
engine/server.py      the receiver: HTTP endpoints (api-contract.md) +
                      WebSocket door the Android app actually uses
engine/screen.py      the CLI (serve / screen / status / overlay / kill / enable)
tests/selftest.py     boots the real receiver and plays the phone at it
```

## Run it

```bash
cd ~/workspace/nexus-screen
python3 engine/screen.py serve            # foreground, Ctrl-C stops
```

Config via environment (or `--host` / `--port` on `serve`):

| var                 | default     | notes                                  |
|---------------------|-------------|----------------------------------------|
| `NEXUS_SCREEN_HOST` | `127.0.0.1` | loopback. On the Nexus box, bind the Tailscale IP (or `0.0.0.0` behind the tailnet) so the phone can reach it. |
| `NEXUS_SCREEN_PORT` | `18789`     | the port the phone's `Config.kt` points at |

## Transport — read this before touching the app

The contract (`api-contract.md`) describes HTTP POSTs, but the shipped
Android client holds **one persistent WebSocket** and never makes HTTP
calls. The receiver serves both on the same port:

- `GET  /nexus/screen` (WebSocket upgrade) — the app's door. Screen events
  in; overlay updates, kill, and (Phase 2) actions pushed back out over the
  same socket. Queued commands flush automatically when the phone
  reconnects and identifies itself.
- `POST /nexus/screen/event` — contract HTTP ingest (scripts, future clients)
- `GET  /nexus/screen/state?device_id=…` — awareness query (all devices if omitted)
- `GET  /nexus/screen/commands?device_id=…` — drain a device's pending queue
- `POST /nexus/screen/overlay` `{device_id, avatar_mood, banner, toolkit}`
- `POST /nexus/screen/kill` `{device_id}` — see kill switch below
- `POST /nexus/screen/enable` `{device_id}`
- `GET  /nexus/screen/health`

## CLI

```bash
python3 engine/screen.py screen [--device ID]     # what's on his screen right now?
python3 engine/screen.py status                   # receiver health + all devices
python3 engine/screen.py overlay --device ID --mood alert --banner "Eyes up"
python3 engine/screen.py kill --device ID         # drop screen access NOW
python3 engine/screen.py enable --device ID       # lift the server-side drop
```

All commands except `serve` talk to the running receiver over loopback.

## Behavior notes

- **Coalescing:** max 1 accepted event/sec per device. Extra events inside
  the window still refresh the state (latest text wins) but are counted as
  `events_coalesced` and never count as meaningful.
- **Meaningful changes** (the only ones that should wake the agent loop):
  `app_foreground`, `notification`. `text_change` / `ui_tap` update state
  quietly.
- **Password safety:** the app strips password nodes on-device; as a second
  line of defense, any event whose payload marks a password field
  (`focused_field` containing "password", or `is_password` /
  `password_field` set) has its `visible_text` dropped here, never stored.
- **Kill switch:** `kill` sends `{type: "kill", scope: "screen_access"}`
  to the phone (pushed live, or queued for reconnect) *and* blocks the
  device server-side — its events get HTTP 403 / are dropped off the
  socket until `enable`. Server-side `enable` only re-opens the receiver;
  re-enabling on the phone is his alone, on the device.
- **Overlay fields:** the current Android client reads `avatar_mood` and
  `banner` only; `toolkit` is sent per contract but ignored by the app
  until its overlay UI grows one.

## Test

```bash
python3 tests/selftest.py    # 15 checks: state, query, coalescing, password
                             # redaction, overlay queue + push, kill, enable
```
