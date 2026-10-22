#!/usr/bin/env python3
"""
Nexus health check — run ON the Nexus box (the one the phone dials).

Answers three questions with numbers, not vibes:
  1. Is the gateway/receiver alive, and how fast does it answer?
  2. Is the model (Ollama) alive, and how fast does it think (tokens/sec)?
  3. Does the box have headroom (RAM + load) while doing it?

Usage:
    python3 nexus_health.py [--model NAME] [--receiver-port 18789] [--ollama-port 11434]

Exit code 0 = all green, 1 = something is down.
Stdlib only. No polling, no loop — one shot, run it whenever.
"""

import argparse
import json
import sys
import time
import urllib.request
import urllib.error


def timed_get(url, timeout=5):
    start = time.time()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read()
            ms = (time.time() - start) * 1000
            return resp.status, body, ms, None
    except Exception as e:  # noqa: BLE001 - report any failure plainly
        ms = (time.time() - start) * 1000
        return None, b"", ms, str(e)


def timed_post_json(url, payload, timeout=120):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            ms = (time.time() - start) * 1000
            return resp.status, json.loads(body or b"{}"), ms, None
    except Exception as e:  # noqa: BLE001
        ms = (time.time() - start) * 1000
        return None, {}, ms, str(e)


def headroom():
    """RAM + load from /proc — no external tools needed."""
    out = {}
    try:
        with open("/proc/meminfo") as f:
            mem = {}
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    mem[parts[0].rstrip(":")] = int(parts[1])  # kB
        total = mem.get("MemTotal", 0)
        avail = mem.get("MemAvailable", 0)
        if total:
            out["ram"] = f"{avail // 1024} MB free of {total // 1024} MB ({avail * 100 // total}% available)"
    except OSError:
        pass
    try:
        with open("/proc/loadavg") as f:
            out["load"] = f"load average {f.read().split()[0]} (1 min)"
    except OSError:
        pass
    return out


def main():
    ap = argparse.ArgumentParser(description="Nexus gateway + model health check")
    ap.add_argument("--model", default=None, help="Ollama model to bench (default: first installed)")
    ap.add_argument("--receiver-port", type=int, default=18789)
    ap.add_argument("--ollama-port", type=int, default=11434)
    args = ap.parse_args()

    ok = True
    print("== NEXUS HEALTH ==")

    # 1. Receiver / gateway health
    url = f"http://127.0.0.1:{args.receiver_port}/health"
    status, _body, ms, err = timed_get(url)
    if status == 200:
        print(f"GATEWAY   UP    {url} answered in {ms:.0f} ms")
    else:
        ok = False
        print(f"GATEWAY   DOWN  {url} -> {err or status}")

    # 2. Ollama alive + model list
    tags_url = f"http://127.0.0.1:{args.ollama_port}/api/tags"
    status, body, ms, err = timed_get(tags_url)
    models = []
    if status == 200:
        try:
            models = [m["name"] for m in json.loads(body).get("models", [])]
        except (ValueError, KeyError):
            pass
        print(f"MODEL API UP    {tags_url} answered in {ms:.0f} ms; models: {', '.join(models) or 'none installed'}")
    else:
        ok = False
        print(f"MODEL API DOWN  {tags_url} -> {err or status}")

    # 3. Bench: one tiny real prompt, timed. eval_duration gives true tok/s.
    bench_model = args.model or (models[0] if models else None)
    if bench_model:
        gen_url = f"http://127.0.0.1:{args.ollama_port}/api/generate"
        status, resp, ms, err = timed_post_json(gen_url, {
            "model": bench_model,
            "prompt": "Reply with exactly one word: ready",
            "stream": False,
            "options": {"num_predict": 8},
        })
        if status == 200 and resp:
            eval_count = resp.get("eval_count", 0)
            eval_ns = resp.get("eval_duration", 0)
            tok_s = (eval_count / (eval_ns / 1e9)) if eval_ns else 0.0
            total_s = resp.get("total_duration", 0) / 1e9
            print(f"BENCH     {bench_model}: {tok_s:.1f} tokens/sec, full reply in {total_s:.1f}s, "
                  f"answer: {resp.get('response', '').strip()!r}")
            if tok_s and tok_s < 3:
                print("          note: under 3 tok/s on CPU is expected for big models — usable for "
                      "short answers, slow for long ones.")
        else:
            ok = False
            print(f"BENCH     FAILED for {bench_model} -> {err or status}")
    elif models == []:
        print("BENCH     skipped — no model installed to test")

    # 4. Headroom
    hr = headroom()
    if hr.get("ram"):
        print(f"RAM       {hr['ram']}")
    if hr.get("load"):
        print(f"CPU       {hr['load']}")

    print("VERDICT:", "ALL GREEN" if ok else "SOMETHING IS DOWN — see lines above")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
