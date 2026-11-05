"""Backend for the cross-node test app.

GET /        -> reads one row from Postgres on the other worker Pi (the cross-node call).
GET /healthz -> process liveness only; never touches the DB (see README for why).

Every request to / writes one JSON log line, so the diagnosis agent can parse
`kubectl logs` instead of scraping free-form text.
"""

import json
import logging
import os
import socket
import sys
import time

import psycopg2
from flask import Flask, jsonify

DB_HOST = os.getenv("DB_HOST", "db-service")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
CONNECT_TIMEOUT = int(os.getenv("DB_CONNECT_TIMEOUT", "3"))
NODE_NAME = os.getenv("NODE_NAME", "unknown")
POD_NAME = os.getenv("POD_NAME", socket.gethostname())

logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
log = logging.getLogger("backend")

app = Flask(__name__)


def _log(event, **fields):
    log.info(json.dumps({"ts": time.time(), "event": event, "pod": POD_NAME,
                         "node": NODE_NAME, **fields}))


@app.route("/")
def index():
    start = time.monotonic()
    phase = "connect"  # which step failed: connect (network/DNS/Service) or query (DB itself)
    try:
        conn = psycopg2.connect(
            host=DB_HOST,
            port=DB_PORT,
            dbname=os.getenv("POSTGRES_DB", "appdb"),
            user=os.getenv("POSTGRES_USER", "postgres_user"),
            password=os.getenv("POSTGRES_PASSWORD", "password"),
            connect_timeout=CONNECT_TIMEOUT,
        )
        phase = "query"
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SELECT value FROM message WHERE id = 1;")
                value = cur.fetchone()[0]
        finally:
            conn.close()
        elapsed = round((time.monotonic() - start) * 1000, 1)
        _log("db_ok", db_host=DB_HOST, elapsed_ms=elapsed)
        return jsonify({"value": value, "elapsed_ms": elapsed, "backend_node": NODE_NAME}), 200
    except Exception as exc:
        elapsed = round((time.monotonic() - start) * 1000, 1)
        error = str(exc).strip()
        _log("db_error", db_host=DB_HOST, phase=phase, elapsed_ms=elapsed,
             error_type=type(exc).__name__, error=error)
        return jsonify({"error": error, "phase": phase, "elapsed_ms": elapsed,
                        "backend_node": NODE_NAME}), 503


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
