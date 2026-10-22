#!/usr/bin/env python3
"""Module 5 Phase 1 server self-test.

Boots the real receiver on a throwaway port, then plays the phone:
contract events over HTTP, and a raw WebSocket session exactly like
NexusClient.kt (events in, overlay + kill pushed back out).

Verifies:
  1. screen events update state; the awareness query returns the screen
  2. text_change is quiet (never "meaningful"); app_foreground wakes
  3. 1 event/sec coalescing: excess events counted, state still fresh
  4. password-field events never store visible_text
  5. overlay commands queue for an offline device and drain via GET
  6. over a live socket: overlay is pushed, kill is pushed, a killed
     device's events are blocked (HTTP 403 + socket drop), enable restores

Run:  python3 tests/selftest.py     (exit 0 = all green)
"""
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
SCREEN_PY = os.path.join(HERE, "..", "engine", "screen.py")
HOST, PORT = "127.0.0.1", 18799
BASE = "http://%s:%d" % (HOST, PORT)

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print("%s %s%s" % ("PASS" if cond else "FAIL", name,
                         (" — " + detail) if detail and not cond else ""))


# ----------------------------------------------------------------------
# HTTP helpers
# ----------------------------------------------------------------------
def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def event(device, ev="text_change", app="com.example.app", text="hello",
          focused="none", ts=None):
    return {"type": "screen_event", "device_id": device,
            "ts": ts if ts is not None else int(time.time()),
            "app": app, "event": ev,
            "payload": {"visible_text": text, "focused_field": focused}}


# ----------------------------------------------------------------------
# minimal WebSocket client (stdlib) — the phone's side of the socket
# ----------------------------------------------------------------------
class WSClient:
    def __init__(self):
        self.sock = socket.create_connection((HOST, PORT), timeout=5)
        self.buf = b""
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            "GET /nexus/screen HTTP/1.1\r\nHost: %s:%d\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
            % (HOST, PORT, key)).encode())
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("handshake failed")
            self.buf += chunk
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        assert b"101" in head.split(b"\r\n")[0], head

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("socket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send_text(self, text):
        data = text.encode()
        mask = os.urandom(4)
        head = bytearray([0x81])
        n = len(data)
        if n < 126:
            head.append(0x80 | n)
        elif n <= 0xFFFF:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(head) + mask + masked)

    def send_json(self, obj):
        self.send_text(json.dumps(obj))

    def recv_json(self, timeout=5):
        self.sock.settimeout(timeout)
        while True:
            h = self._read(2)
            op = h[0] & 0x0F
            ln = h[1] & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._read(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(ln) if ln else b""
            if op == 0x9:   # ping -> pong (masked, like a real client)
                self.send_text("")  # liveness only; server tolerates
                continue
            if op == 0x8:
                raise ConnectionError("server closed socket")
            if op == 0x1:
                return json.loads(payload.decode())

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# ----------------------------------------------------------------------
# the tests
# ----------------------------------------------------------------------
def main():
    proc = subprocess.Popen(
        [sys.executable, SCREEN_PY, "serve", "--host", HOST,
         "--port", str(PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                code, _ = call("GET", "/nexus/screen/health")
                if code == 200:
                    break
            except Exception:
                pass
            time.sleep(0.1)
        else:
            check("receiver boots", False, "health endpoint never answered")
            return 1
        check("receiver boots", True)

        # 1 — state updates + awareness query
        code, r = call("POST", "/nexus/screen/event",
                       event("test-a", "app_foreground",
                             "com.android.chrome", "Nexus home page"))
        check("app_foreground accepted + meaningful",
              code == 200 and r.get("meaningful") is True, str(r))
        code, st = call("GET", "/nexus/screen/state?device_id=test-a")
        check("query returns current screen",
              code == 200 and st["app"] == "com.android.chrome"
              and st["visible_text"] == "Nexus home page"
              and "summary" in st, str(st))

        # 2 — text_change after the coalesce window: accepted but quiet
        time.sleep(1.1)
        code, r = call("POST", "/nexus/screen/event",
                       event("test-a", "text_change",
                             "com.android.chrome", "typed something"))
        check("text_change accepted, NOT meaningful",
              code == 200 and r.get("status") == "accepted"
              and r.get("meaningful") is False, str(r))

        # 3 — coalescing: 3 rapid events, only the first counts
        for i in range(3):
            code, r = call("POST", "/nexus/screen/event",
                           event("test-rate", "text_change",
                                 "com.example", "burst %d" % i))
        code, st = call("GET", "/nexus/screen/state?device_id=test-rate")
        check("1/sec coalescing (1 accepted, 2 coalesced)",
              st["events_accepted"] == 1 and st["events_coalesced"] == 2,
              str({k: st[k] for k in ("events_accepted",
                                      "events_coalesced")}))
        check("coalesced events still refresh state",
              st["visible_text"] == "burst 2", st.get("visible_text", ""))

        # 4 — password field: text must NOT be stored
        code, r = call("POST", "/nexus/screen/event",
                       event("test-pw", "text_change", "com.bank.app",
                             "hunter2", focused="password"))
        code, st = call("GET", "/nexus/screen/state?device_id=test-pw")
        check("password-field visible_text dropped",
              r.get("password_redacted") is True
              and st["visible_text"] is None
              and "hunter2" not in json.dumps(st), str(st))

        # 5 — overlay queues for an offline device, drains via GET
        code, r = call("POST", "/nexus/screen/overlay",
                       {"device_id": "test-ov", "avatar_mood": "alert",
                        "banner": "Eyes up"})
        code, out = call("GET", "/nexus/screen/commands?device_id=test-ov")
        cmds = out.get("commands", [])
        check("overlay queued + retrievable",
              len(cmds) == 1 and cmds[0]["type"] == "overlay"
              and cmds[0]["avatar_mood"] == "alert"
              and cmds[0]["banner"] == "Eyes up", str(out))
        code, out = call("GET", "/nexus/screen/commands?device_id=test-ov")
        check("queue empty after drain", out.get("commands") == [])

        # 6 — live socket: push overlay, push kill, killed = blocked
        ws = WSClient()
        ws.send_json(event("test-ws", "app_foreground", "com.example.ws",
                           "WS hello"))
        time.sleep(0.3)
        code, st = call("GET", "/nexus/screen/state?device_id=test-ws")
        check("socket event lands in state",
              st.get("visible_text") == "WS hello", str(st))

        code, r = call("POST", "/nexus/screen/overlay",
                       {"device_id": "test-ws", "avatar_mood": "working",
                        "banner": "On it"})
        pushed = ws.recv_json()
        check("overlay pushed over the socket",
              r.get("pushed") is True and pushed.get("type") == "overlay"
              and pushed.get("avatar_mood") == "working", str(pushed))

        code, r = call("POST", "/nexus/screen/kill",
                       {"device_id": "test-ws"})
        killmsg = ws.recv_json()
        check("kill pushed over the socket",
              killmsg == {"type": "kill", "scope": "screen_access"},
              str(killmsg))

        code, _ = call("POST", "/nexus/screen/event",
                       event("test-ws", "text_change", "com.example.ws",
                             "should not land"))
        check("killed device: HTTP event rejected (403)", code == 403)

        ws.send_json(event("test-ws", "text_change", "com.example.ws",
                           "also should not land"))
        time.sleep(0.4)
        code, st = call("GET", "/nexus/screen/state?device_id=test-ws")
        check("killed device: socket event dropped, state frozen",
              st["visible_text"] == "WS hello"
              and st["events_rejected"] >= 2, str(st))

        call("POST", "/nexus/screen/enable", {"device_id": "test-ws"})
        time.sleep(1.1)  # step outside the coalesce window
        code, r = call("POST", "/nexus/screen/event",
                       event("test-ws", "text_change", "com.example.ws",
                             "back again"))
        code, st = call("GET", "/nexus/screen/state?device_id=test-ws")
        check("enable restores the flow",
              code == 200 and st["visible_text"] == "back again", str(st))
        ws.close()

        failed = [n for n, ok in RESULTS if not ok]
        print("\n%d/%d checks passed%s" % (
            len(RESULTS) - len(failed), len(RESULTS),
            (" — FAILED: " + ", ".join(failed)) if failed else ""))
        return 1 if failed else 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
