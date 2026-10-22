#!/usr/bin/env python3
"""Nexus Screen — Module 5 Phase 1 (server side). The eyes' other end.

  screen.py serve [--host H] [--port P]   run the event receiver (foreground)
  screen.py screen [--device ID]          what's on his screen right now?
  screen.py status                        receiver health + known devices
  screen.py overlay [--device ID] [--mood idle|listening|working|alert]
                    [--banner "text"] [--toolkit voice,screen,kill]
                                          push an overlay update to the phone
  screen.py kill [--device ID]            kill switch: drop screen access NOW
  screen.py enable [--device ID]          lift the server-side drop

Everything except `serve` is a thin client of the running receiver
(loopback HTTP). Defaults: host 127.0.0.1, port 18789 — override with
NEXUS_SCREEN_HOST / NEXUS_SCREEN_PORT or the flags above.
"""
import json
import os
import sys
import urllib.request
import urllib.error
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ENV_HOST = os.environ.get("NEXUS_SCREEN_HOST", "127.0.0.1")
ENV_PORT = int(os.environ.get("NEXUS_SCREEN_PORT", "18789"))


def _client_base(host, port):
    # a daemon bound to 0.0.0.0 is reached from here via loopback
    h = host if host not in ("0.0.0.0", "::") else "127.0.0.1"
    return "http://%s:%d" % (h, port)


def _call(method, path, body=None, host=ENV_HOST, port=ENV_PORT):
    url = _client_base(host, port) + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {"error": str(e)}
    except (urllib.error.URLError, OSError):
        return None, {"error": "receiver not running — start it with: "
                               "screen.py serve"}


def _flags(args):
    """Tiny --key value / --flag parser, in the other modules' spirit."""
    out, rest = {}, []
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--") and i + 1 < len(args) \
                and not args[i + 1].startswith("--"):
            out[a[2:]] = args[i + 1]
            i += 2
        elif a.startswith("--"):
            out[a[2:]] = True
            i += 1
        else:
            rest.append(a)
            i += 1
    return out, rest


def _print(obj):
    print(json.dumps(obj, indent=1))


def cmd_serve(flags):
    import server
    host = flags.get("host", ENV_HOST)
    port = int(flags.get("port", ENV_PORT))
    server.run(host, port)


def cmd_screen(flags):
    device = flags.get("device")
    path = "/nexus/screen/state"
    if device:
        path += "?device_id=%s" % urllib.parse.quote(str(device))
    code, out = _call("GET", path)
    if code == 200 and not device and "devices" in out:
        devs = out["devices"]
        if len(devs) == 1:
            out = devs[0]
        elif not devs:
            out = {"note": "no device has reported a screen yet"}
    _print(out)


def cmd_status(flags):
    code, out = _call("GET", "/nexus/screen/health")
    if code == 200:
        _, states = _call("GET", "/nexus/screen/state")
        out["devices"] = states.get("devices", [])
    _print(out)


def cmd_overlay(flags):
    body = {"device_id": flags.get("device"),
            "avatar_mood": flags.get("mood", "idle"),
            "banner": flags.get("banner")}
    if flags.get("toolkit"):
        body["toolkit"] = [t.strip() for t in
                            str(flags["toolkit"]).split(",") if t.strip()]
    if not body["device_id"]:
        print("overlay needs --device ID")
        return
    code, out = _call("POST", "/nexus/screen/overlay", body)
    _print(out)


def cmd_kill(flags):
    if not flags.get("device"):
        print("kill needs --device ID")
        return
    code, out = _call("POST", "/nexus/screen/kill",
                      {"device_id": flags["device"]})
    _print(out)


def cmd_enable(flags):
    if not flags.get("device"):
        print("enable needs --device ID")
        return
    code, out = _call("POST", "/nexus/screen/enable",
                      {"device_id": flags["device"]})
    _print(out)


COMMANDS = {"serve": cmd_serve, "screen": cmd_screen, "status": cmd_status,
            "overlay": cmd_overlay, "kill": cmd_kill, "enable": cmd_enable}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        return
    flags, _ = _flags(sys.argv[2:])
    COMMANDS[sys.argv[1]](flags)


if __name__ == "__main__":
    main()
