#!/usr/bin/env python3
"""Nexus Screen Receiver — Module 5 Phase 1 (server side).

The door events walk through. One light stdlib process, two doors in:

  * WebSocket  GET  /nexus/screen            — the door the Android app
    actually uses (NexusClient.kt holds one persistent socket: screen
    events in, overlay/kill pushed back out over the same socket).
  * HTTP       POST /nexus/screen/event      — the api-contract.md endpoint,
    same ingest path, for scripts and future clients.
               GET  /nexus/screen/state      — awareness query
               GET  /nexus/screen/commands   — drain a device's command queue
               POST /nexus/screen/overlay    — queue/push an overlay update
               POST /nexus/screen/kill       — kill switch (one device)
               POST /nexus/screen/enable     — lift the server-side drop
               GET  /nexus/screen/health     — liveness + device count

Binds loopback by default: the phone reaches it over Tailscale, never the
public internet. Config via env: NEXUS_SCREEN_HOST / NEXUS_SCREEN_PORT
(default port 18789 — the port Config.kt on the phone points at).

Event-driven only: nothing here polls, ticks, or records. State lives in
memory (awareness.py) and dies with the process, by design.
"""
import base64
import hashlib
import json
import os
import struct
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import awareness

ENGINE = awareness.AwarenessEngine()

DEFAULT_HOST = os.environ.get("NEXUS_SCREEN_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("NEXUS_SCREEN_PORT", "18789"))

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_FRAME = 1 << 20  # 1 MB — contract events are ~4 KB; this is a guardrail


def log(msg):
    sys.stderr.write("[nexus-screen] %s\n" % msg)
    sys.stderr.flush()


# ----------------------------------------------------------------------
# live WebSocket connections, by device_id
# ----------------------------------------------------------------------
class WSConnection:
    """One device's socket. Sends are locked: HTTP handler threads push
    overlay/kill while the socket's own thread is busy reading."""

    def __init__(self, sock):
        self._sock = sock
        self._send_lock = threading.Lock()

    def send_text(self, text):
        data = text.encode("utf-8")
        head = bytearray([0x81])  # FIN + text opcode
        n = len(data)
        if n < 126:
            head.append(n)
        elif n <= 0xFFFF:
            head.append(126)
            head += struct.pack(">H", n)
        else:
            head.append(127)
            head += struct.pack(">Q", n)
        with self._send_lock:
            try:
                self._sock.sendall(bytes(head) + data)
                return True
            except OSError:
                return False

    def send_json(self, obj):
        return self.send_text(json.dumps(obj))

    def send_frame(self, opcode, payload=b""):
        head = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head.append(n)
        elif n <= 0xFFFF:
            head.append(126)
            head += struct.pack(">H", n)
        else:
            head.append(127)
            head += struct.pack(">Q", n)
        with self._send_lock:
            try:
                self._sock.sendall(bytes(head) + payload)
                return True
            except OSError:
                return False


class WSRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._conns = {}   # device_id -> WSConnection

    def register(self, device_id, conn):
        with self._lock:
            self._conns[device_id] = conn

    def unregister(self, device_id, conn):
        with self._lock:
            if self._conns.get(device_id) is conn:
                del self._conns[device_id]

    def get(self, device_id):
        with self._lock:
            return self._conns.get(device_id)


REGISTRY = WSRegistry()


def flush_queue(device_id):
    """Push whatever is queued for a device over its live socket, if any.
    Returns {"queued": bool, "pushed": bool}: queued=True means commands
    are still waiting (device offline — they go out on reconnect, or via a
    GET /nexus/screen/commands drain)."""
    conn = REGISTRY.get(device_id)
    if conn is None:
        return {"queued": ENGINE.pending_count(device_id) > 0,
                "pushed": False}
    pending = ENGINE.drain_commands(device_id)
    pushed = 0
    for i, cmd in enumerate(pending):
        if conn.send_json(cmd):
            pushed += 1
        else:  # socket died mid-flush — put the rest back
            for rest in pending[i:]:
                ENGINE.queue_command(device_id, rest)
            break
    return {"queued": pushed < len(pending), "pushed": pushed > 0}


def deliver(device_id, command):
    """Queue a server->device command, then push immediately if the
    device's socket is live."""
    ENGINE.queue_command(device_id, command)
    return flush_queue(device_id)


# ----------------------------------------------------------------------
# WebSocket framing (RFC 6455, the small honest subset the app needs)
# ----------------------------------------------------------------------
def _read_exact(rfile, n):
    buf = b""
    while len(buf) < n:
        chunk = rfile.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _read_frame(rfile):
    """Returns (opcode, payload) or None on EOF. Reassembles fragments."""
    payload = b""
    opcode = None
    while True:
        head = _read_exact(rfile, 2)
        if head is None:
            return None
        fin = head[0] & 0x80
        op = head[0] & 0x0F
        masked = head[1] & 0x80
        length = head[1] & 0x7F
        if length == 126:
            ext = _read_exact(rfile, 2)
            if ext is None:
                return None
            length = struct.unpack(">H", ext)[0]
        elif length == 127:
            ext = _read_exact(rfile, 8)
            if ext is None:
                return None
            length = struct.unpack(">Q", ext)[0]
        if length > MAX_FRAME:
            return None
        mask = _read_exact(rfile, 4) if masked else None
        if masked and mask is None:
            return None
        data = _read_exact(rfile, length) if length else b""
        if data is None:
            return None
        if mask:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if opcode is None:
            opcode = op
        payload += data
        if fin:
            return opcode, payload


# ----------------------------------------------------------------------
# HTTP + WebSocket handler
# ----------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "NexusScreen/1.0"

    def log_message(self, fmt, *args):  # keep stderr for real events only
        pass

    # ---------- helpers ----------
    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        raw = self.rfile.read(n) if n > 0 else b""
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return None

    # ---------- GET ----------
    def do_GET(self):
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        device_id = (qs.get("device_id") or [None])[0]

        if url.path == "/nexus/screen" and \
                "websocket" in (self.headers.get("Upgrade") or "").lower():
            self._ws_session()
            return
        if url.path == "/nexus/screen/health":
            self._json(200, {"ok": True, "devices": len(ENGINE.devices())})
            return
        if url.path == "/nexus/screen/state":
            if device_id:
                st = ENGINE.current_screen(device_id)
                if st is None:
                    self._json(404, {"error": "unknown device"})
                else:
                    self._json(200, st)
            else:
                self._json(200, {"devices": ENGINE.devices()})
            return
        if url.path == "/nexus/screen/commands":
            if not device_id:
                self._json(400, {"error": "device_id required"})
                return
            self._json(200, {"device_id": device_id,
                             "commands": ENGINE.drain_commands(device_id)})
            return
        self._json(404, {"error": "not found"})

    # ---------- POST ----------
    def do_POST(self):
        url = urlparse(self.path)

        if url.path == "/nexus/screen/event":
            msg = self._body_json()
            if msg is None:
                self._json(400, {"error": "bad json"})
                return
            status, info = ENGINE.ingest(msg)
            if status == "invalid":
                self._json(400, info)
            elif status == "killed":
                self._json(403, {"error": "screen access killed for device",
                                 **info})
            else:
                if info.get("meaningful"):
                    log("meaningful %s from %s (%s)" % (
                        msg.get("event"), msg.get("device_id"),
                        msg.get("app")))
                self._json(200, {"ok": True, "status": status, **info})
            return

        if url.path == "/nexus/screen/overlay":
            body = self._body_json() or {}
            device_id = body.get("device_id")
            if not device_id:
                self._json(400, {"error": "device_id required"})
                return
            cmd = {"type": "overlay",
                   "avatar_mood": body.get("avatar_mood", "idle"),
                   "toolkit": body.get("toolkit",
                                       ["voice", "what's on screen",
                                        "kill switch"]),
                   "banner": body.get("banner")}
            self._json(200, {"device_id": device_id, **deliver(device_id, cmd)})
            return

        if url.path == "/nexus/screen/kill":
            body = self._body_json() or {}
            device_id = body.get("device_id")
            if not device_id:
                self._json(400, {"error": "device_id required"})
                return
            cmd = ENGINE.kill(device_id)  # marks killed + queues the command
            result = flush_queue(device_id)  # push now if the socket is live
            log("KILL switch engaged for %s" % device_id)
            self._json(200, {"device_id": device_id, "killed": True, **result})
            return

        if url.path == "/nexus/screen/enable":
            body = self._body_json() or {}
            device_id = body.get("device_id")
            if not device_id:
                self._json(400, {"error": "device_id required"})
                return
            log("screen access re-enabled for %s" % device_id)
            self._json(200, ENGINE.enable(device_id))
            return

        self._json(404, {"error": "not found"})

    # ---------- WebSocket session (the Android app's door) ----------
    def _ws_session(self):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self._json(400, {"error": "missing Sec-WebSocket-Key"})
            return
        accept = base64.b64encode(hashlib.sha1(
            (key + WS_GUID).encode("ascii")).digest()).decode("ascii")
        self.connection.sendall((
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Accept: %s\r\n\r\n" % accept).encode("ascii"))
        self.close_connection = True

        conn = WSConnection(self.connection)
        device_id = None
        log("websocket connected from %s" % (self.client_address,))
        try:
            while True:
                frame = _read_frame(self.rfile)
                if frame is None:
                    break
                opcode, payload = frame
                if opcode == 0x8:            # close
                    conn.send_frame(0x8)
                    break
                if opcode == 0x9:            # ping -> pong
                    conn.send_frame(0xA, payload)
                    continue
                if opcode != 0x1:            # only text carries our JSON
                    continue
                try:
                    msg = json.loads(payload.decode("utf-8"))
                except Exception:
                    continue
                if msg.get("type") != "screen_event":
                    continue  # action_ack etc. land in Phase 2
                status, info = ENGINE.ingest(msg)
                dev = msg.get("device_id")
                if dev and dev != device_id:
                    device_id = dev
                    REGISTRY.register(device_id, conn)
                    log("device identified: %s" % device_id)
                if status == "killed":
                    log("dropped event from killed device %s" % device_id)
                elif device_id:
                    # flush anything queued while the phone was away
                    flush_queue(device_id)
        except (OSError, ConnectionError):
            pass
        finally:
            if device_id:
                REGISTRY.unregister(device_id, conn)
            log("websocket closed (%s)" % (device_id or "unidentified"))


def run(host=DEFAULT_HOST, port=DEFAULT_PORT):
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    log("listening on %s:%d — POST /nexus/screen/event | WS /nexus/screen"
        % (host, port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
        httpd.shutdown()


if __name__ == "__main__":
    run()
