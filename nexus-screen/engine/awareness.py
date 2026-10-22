#!/usr/bin/env python3
"""Nexus Screen Awareness — Module 5 Phase 1 (server side).

The awareness engine. Raw screen events from his phone land here and become
one small piece of living state per device: what's on his screen RIGHT NOW.
No history, no recordings — when a new event arrives, the old state is
replaced. The Soul (and the shell) query it like memory:

    engine.current_screen("moto-g-2025")
    -> {"app": "com.android.chrome", "visible_text": "...", ...}

Anti-loop rules live here too (server-notes.md, for the 2-vCPU box):
  * Coalescing: at most 1 accepted event/sec per device. Extra events inside
    the window still refresh the state (latest text wins) but are counted as
    coalesced and can never count as meaningful.
  * Meaningful changes: only app_foreground (the contract's app_switch),
    notification, and explicit wake pings wake the agent loop. Plain
    text_change / ui_tap events update state quietly.

Password safety (defense in depth — the Android service already strips
password nodes on-device): if an event's payload marks a password field,
its visible_text is dropped on the floor here and never stored.
"""
import threading
import time

MAX_TEXT_CHARS = 4000          # contract: visible_text truncated at 4k chars
MIN_EVENT_GAP_S = 1.0          # contract: max 1 event/sec per device

# Event types that count as a meaningful change (wake the agent loop).
# "app_switch" is the brief's word; the wire name in api-contract.md and the
# Android service is "app_foreground". Both are accepted.
MEANINGFUL_EVENTS = {"app_foreground", "app_switch", "notification", "wake"}


def _looks_like_password(payload):
    """True when the payload indicates the focused field is a password."""
    if not isinstance(payload, dict):
        return False
    if payload.get("is_password") or payload.get("password_field"):
        return True
    focused = str(payload.get("focused_field") or "").lower()
    return "password" in focused


class AwarenessEngine:
    """In-memory per-device screen state + outbound command queues.

    Thread-safe: the receiver serves HTTP and WebSocket connections on
    separate threads, all feeding this one engine.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._devices = {}   # device_id -> state dict (incl. private _keys)
        self._queues = {}    # device_id -> [server->device command dicts]

    # ------------------------------------------------------------------
    # device -> server: event ingest
    # ------------------------------------------------------------------
    def ingest(self, msg):
        """Take one contract screen_event. Returns (status, info).

        status: "accepted" | "coalesced" | "killed" | "invalid"
        info:   details incl. whether the event was meaningful (a wake).
        """
        if not isinstance(msg, dict) or msg.get("type") != "screen_event":
            return "invalid", {"error": "type must be screen_event"}
        device_id = msg.get("device_id")
        if not device_id:
            return "invalid", {"error": "device_id missing"}

        now_wall = time.time()
        now_mono = time.monotonic()
        with self._lock:
            st = self._devices.get(device_id)
            if st is None:
                st = self._blank_state(device_id)
                self._devices[device_id] = st

            if st["killed"]:
                st["events_rejected"] += 1
                return "killed", {"device_id": device_id}

            payload = msg.get("payload") or {}
            event = str(msg.get("event") or "text_change")
            st["events_total"] += 1

            # Coalescing window: first event in each 1s window is accepted;
            # later ones still update state but stay quiet (see docstring).
            accepted = (now_mono - st["_last_accept_mono"]) >= MIN_EVENT_GAP_S
            if accepted:
                st["_last_accept_mono"] = now_mono
                st["events_accepted"] += 1
            else:
                st["events_coalesced"] += 1

            # Password safety: never store text from a password field.
            redacted = _looks_like_password(payload)
            if redacted:
                st["password_redacted"] += 1
                visible_text = None
            else:
                visible_text = payload.get("visible_text")
                if isinstance(visible_text, str):
                    visible_text = visible_text[:MAX_TEXT_CHARS]
                elif visible_text is not None:
                    visible_text = str(visible_text)[:MAX_TEXT_CHARS]

            st["app"] = msg.get("app")
            st["visible_text"] = visible_text
            st["focused_field"] = payload.get("focused_field")
            st["last_event"] = event
            st["device_ts"] = msg.get("ts")
            st["updated_at"] = now_wall

            meaningful = bool(accepted and event in MEANINGFUL_EVENTS)
            if meaningful:
                st["meaningful_events"] += 1
                st["last_meaningful_event"] = event
                st["last_meaningful_at"] = now_wall

            return ("accepted" if accepted else "coalesced"), {
                "device_id": device_id,
                "meaningful": meaningful,
                "password_redacted": redacted,
            }

    # ------------------------------------------------------------------
    # queries — "what's on his screen right now?"
    # ------------------------------------------------------------------
    def current_screen(self, device_id):
        """Public state for one device, or None if it never reported."""
        with self._lock:
            st = self._devices.get(device_id)
            return self._public(st) if st else None

    def devices(self):
        """Summaries for every known device."""
        with self._lock:
            return [self._public(st) for st in self._devices.values()]

    @staticmethod
    def _blank_state(device_id):
        return {
            "device_id": device_id,
            "app": None,
            "visible_text": None,
            "focused_field": None,
            "last_event": None,
            "device_ts": None,
            "updated_at": None,
            "killed": False,
            "events_total": 0,
            "events_accepted": 0,
            "events_coalesced": 0,
            "events_rejected": 0,
            "meaningful_events": 0,
            "password_redacted": 0,
            "last_meaningful_event": None,
            "last_meaningful_at": None,
            "_last_accept_mono": 0.0,
        }

    @staticmethod
    def _public(st):
        out = {k: v for k, v in st.items() if not k.startswith("_")}
        if st["updated_at"]:
            out["age_s"] = round(time.time() - st["updated_at"], 1)
            text = st["visible_text"] or ""
            preview = (text[:80] + "…") if len(text) > 80 else text
            out["summary"] = "%s — %r — focused: %s — updated %.0fs ago" % (
                st["app"] or "?", preview, st["focused_field"] or "none",
                out["age_s"])
        else:
            out["age_s"] = None
            out["summary"] = "%s — no screen event yet" % st["device_id"]
        return out

    # ------------------------------------------------------------------
    # server -> device: command queue (overlay / kill / future actions)
    # ------------------------------------------------------------------
    def queue_command(self, device_id, command):
        """Append a server->device command; returns the queue depth."""
        with self._lock:
            q = self._queues.setdefault(device_id, [])
            q.append(command)
            return len(q)

    def drain_commands(self, device_id):
        """Pop every queued command for a device (delivery = drain)."""
        with self._lock:
            return self._queues.pop(device_id, [])

    def pending_count(self, device_id):
        with self._lock:
            return len(self._queues.get(device_id, []))

    # ------------------------------------------------------------------
    # kill switch
    # ------------------------------------------------------------------
    def kill(self, device_id):
        """Drop screen access for a device: block its events server-side and
        queue the contract kill message for the device itself."""
        cmd = {"type": "kill", "scope": "screen_access"}
        with self._lock:
            st = self._devices.get(device_id)
            if st is None:
                st = self._blank_state(device_id)
                self._devices[device_id] = st
            st["killed"] = True
            self._queues.setdefault(device_id, []).append(cmd)
        return cmd

    def enable(self, device_id):
        """Re-open the receiver for a device (the phone-side re-enable is
        his alone, on the device — this only lifts the server-side drop)."""
        with self._lock:
            st = self._devices.get(device_id)
            if st is None:
                st = self._blank_state(device_id)
                self._devices[device_id] = st
            st["killed"] = False
        return {"device_id": device_id, "killed": False}
