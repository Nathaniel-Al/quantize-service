"""
storage.py

Minimal SQLite-backed persistence for freeze records, keyed by freezeId.
Using SQLite (stdlib, file-backed) instead of a plain in-process dict so
state is consistent even if the WSGI server runs multiple worker
processes -- a dict would be per-process and silently lose "conflict"
detection / replay behavior across workers.

Each row stores:
  - freeze_id      (primary key)
  - request_json   the exact parsed JSON body of the freeze request that
                    established this freezeId (used to detect identical
                    replay vs. a genuine 409 conflict)
  - response_json  the exact JSON response body returned for this
                    freezeId (returned unchanged on replay, and used as
                    the source of truth for the /quantize select phase's
                    lineage/manifest verification)
"""

import json
import os
import sqlite3
import threading

DB_PATH = os.environ.get("QUANTIZE_DB_PATH", os.path.join(os.getcwd(), "data", "freezes.db"))

_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS freezes (
                freeze_id TEXT PRIMARY KEY,
                request_json TEXT NOT NULL,
                response_json TEXT NOT NULL
            )
            """
        )
        conn.commit()


def get_freeze(freeze_id: str):
    """Return (request_obj, response_obj) or None if not found."""
    with _connect() as conn:
        cur = conn.execute(
            "SELECT request_json, response_json FROM freezes WHERE freeze_id = ?",
            (freeze_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return json.loads(row[0]), json.loads(row[1])


def put_freeze_if_absent(freeze_id: str, request_obj, response_obj):
    """
    Attempt to reserve freeze_id with the given request/response.

    Returns a tuple (outcome, stored_response_obj):
      outcome == "created" : this call reserved the id; stored_response_obj
                              is the response_obj passed in.
      outcome == "replay"  : an identical request was already stored under
                              this id; stored_response_obj is the
                              previously-stored (unchanged) response.
      outcome == "conflict": a different request is already stored under
                              this id; stored_response_obj is the
                              previously-stored response (not to be
                              returned to the caller -- caller should
                              respond with 409 instead).
    """
    with _lock:
        with _connect() as conn:
            cur = conn.execute(
                "SELECT request_json, response_json FROM freezes WHERE freeze_id = ?",
                (freeze_id,),
            )
            row = cur.fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO freezes (freeze_id, request_json, response_json) VALUES (?, ?, ?)",
                    (
                        freeze_id,
                        json.dumps(request_obj, sort_keys=True, ensure_ascii=False),
                        json.dumps(response_obj, ensure_ascii=False),
                    ),
                )
                conn.commit()
                return "created", response_obj

            stored_request = json.loads(row[0])
            stored_response = json.loads(row[1])

            if _canonical(stored_request) == _canonical(request_obj):
                return "replay", stored_response
            else:
                return "conflict", stored_response


def _canonical(obj):
    """Order-independent, type-stable canonical form for equality checks."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)
