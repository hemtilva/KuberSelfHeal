#!/usr/bin/env python3
"""Continuously probe the backend and record the symptom stream.

Run it from OUTSIDE the two worker Pis (the control-plane Pi or a laptop), so it
sees the app the way a user does. Standard library only, no installs needed.

    python3 prober.py --url http://<backend-pi-ip>:30080/ --out probes.jsonl

Each probe appends one JSON line:
    {"ts": ..., "ok": false, "http_status": 503, "latency_ms": 3012.4,
     "where": "backend->db", "phase": "connect", "hint": "timeout",
     "error": "timeout expired", ...}

`where` says which hop failed:
    prober->backend  the NodePort / backend pod itself is unreachable
    backend->db      backend is up, but its cross-node call to Postgres failed
`hint` is a first guess at the layer, which the diagnosis agent will refine.
"""

import argparse
import json
import socket
import sys
import time
import urllib.error
import urllib.request

# (substring in lower-cased error text, hint). First match wins, so order matters.
ERROR_HINTS = [
    ("could not translate host name", "dns"),
    ("name or service not known", "dns"),
    ("temporary failure in name resolution", "dns"),
    ("connection refused", "refused"),          # reached a node, nothing listening: targetPort / no endpoints
    ("actively refused", "refused"),            # Windows wording, if the prober runs on a laptop
    ("no route to host", "no_route"),           # no endpoints (kube-proxy REJECT) or broken routing
    ("network is unreachable", "no_route"),
    ("timeout expired", "timeout"),             # packets lost: firewall, MTU, NetworkPolicy, overlay, loss
    ("timed out", "timeout"),
    ("password authentication failed", "db_auth"),
    ("does not exist", "db_schema"),
    ("the database system is starting up", "db_starting"),
    ("server closed the connection unexpectedly", "conn_reset"),
    ("connection reset", "conn_reset"),
]


def hint_for(error_text):
    text = (error_text or "").lower()
    for needle, hint in ERROR_HINTS:
        if needle in text:
            return hint
    return "unknown"


def probe(url, timeout):
    rec = {"ts": round(time.time(), 3), "url": url}
    start = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            status, body = resp.status, resp.read()
    except urllib.error.HTTPError as exc:  # backend answered with an error code
        status, body = exc.code, exc.read()
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        rec.update(ok=False, http_status=None, where="prober->backend",
                   latency_ms=round((time.monotonic() - start) * 1000, 1),
                   error=str(reason), hint=hint_for(str(reason)))
        return rec

    rec["latency_ms"] = round((time.monotonic() - start) * 1000, 1)
    rec["http_status"] = status
    try:
        data = json.loads(body)
    except ValueError:
        data = {"raw": body[:200].decode(errors="replace")}

    if status == 200 and "value" in data:
        rec.update(ok=True, where=None, hint=None, db_elapsed_ms=data.get("elapsed_ms"))
    else:
        error = data.get("error") or data.get("raw") or f"HTTP {status}"
        rec.update(ok=False, where="backend->db" if status == 503 else "prober->backend",
                   phase=data.get("phase"), db_elapsed_ms=data.get("elapsed_ms"),
                   error=error, hint=hint_for(error))
    rec["backend_node"] = data.get("backend_node")
    return rec


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="e.g. http://<backend-pi-ip>:30080/")
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between probes (default 1)")
    ap.add_argument("--timeout", type=float, default=10.0,
                    help="HTTP timeout; keep it above the backend's DB timeout (default 10)")
    ap.add_argument("--out", default="probes.jsonl", help="JSONL output file (appended)")
    ap.add_argument("--count", type=int, default=0, help="stop after N probes (0 = run forever)")
    args = ap.parse_args()

    sent = failed = 0
    last_state = None
    print(f"probing {args.url} every {args.interval}s -> {args.out}  (Ctrl+C to stop)", file=sys.stderr)
    try:
        with open(args.out, "a", encoding="utf-8") as out:
            while args.count == 0 or sent < args.count:
                tick = time.monotonic()
                rec = probe(args.url, args.timeout)
                out.write(json.dumps(rec) + "\n")
                out.flush()
                sent += 1
                failed += not rec["ok"]

                # Console: one line per probe, plus a marker whenever the state changes.
                state = "OK" if rec["ok"] else f"FAIL {rec['where']} [{rec['hint']}]"
                if state != last_state:
                    print(f"--- state change: {last_state} -> {state}", file=sys.stderr)
                    last_state = state
                detail = "" if rec["ok"] else "  " + " ".join(rec["error"].split())[:100]
                print(f"{time.strftime('%H:%M:%S')}  {state:<34} {rec['latency_ms']:>8.1f} ms{detail}")

                time.sleep(max(0.0, args.interval - (time.monotonic() - tick)))
    except KeyboardInterrupt:
        pass
    if sent:
        print(f"\n{sent} probes, {failed} failed ({100 * failed / sent:.1f}%)", file=sys.stderr)


if __name__ == "__main__":
    main()
