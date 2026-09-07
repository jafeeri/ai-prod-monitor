"""Central SQLite store: one schema, WAL, and a connection helper everyone shares.

Fixing three bugs at the root:
- readers used to crash on a fresh db (`no such table`) → every connection ensures
  the schema (idempotent CREATE IF NOT EXISTS).
- one shared connection across threads collided (`transaction within a transaction`)
  → callers use their own short-lived / thread-local connections instead.
- default rollback-journal serialised a live dashboard read against the app's writes
  → WAL + busy_timeout let readers and the writer proceed concurrently.
"""
from __future__ import annotations

import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    trace_id TEXT PRIMARY KEY, name TEXT, start_ns INTEGER, end_ns INTEGER, meta TEXT
);
CREATE TABLE IF NOT EXISTS spans (
    span_id TEXT PRIMARY KEY, trace_id TEXT, parent_id TEXT, name TEXT, kind TEXT,
    start_ns INTEGER, end_ns INTEGER, latency_ms REAL, model TEXT,
    tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL, attributes TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, span_id TEXT, trace_id TEXT, name TEXT,
    ts_ns INTEGER, attributes TEXT
);
CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT, trace_id TEXT, span_id TEXT UNIQUE, ts_ns INTEGER,
    relevance REAL, faithfulness REAL, safety REAL, overall REAL, reason TEXT, judged_by TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ns INTEGER, metric TEXT, message TEXT, trace_id TEXT
);
CREATE INDEX IF NOT EXISTS ix_spans_trace ON spans(trace_id);
CREATE INDEX IF NOT EXISTS ix_events_span ON events(span_id);
CREATE INDEX IF NOT EXISTS ix_scores_ts   ON scores(ts_ns);
CREATE INDEX IF NOT EXISTS ix_alerts_ts   ON alerts(ts_ns);
"""


_ready: set[str] = set()          # paths whose schema this process has ensured
_ready_lock = threading.Lock()


def connect(db_path: str) -> sqlite3.Connection:
    """A connection with WAL + busy_timeout, schema ensured, row access by name.
    Each caller owns its connection (thread-local for the writer, short-lived for readers)."""
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")     # readers don't block the writer
    conn.execute("PRAGMA synchronous=NORMAL")   # fast + safe enough for a monitor
    conn.execute("PRAGMA busy_timeout=5000")     # wait, don't error, on a brief lock
    # Ensure the schema once per path per process (not on every connect) so reader
    # connections don't take a write lock for a no-op DDL under concurrency.
    if db_path not in _ready:
        with _ready_lock:
            if db_path not in _ready:
                conn.executescript(SCHEMA)       # idempotent → no more 'no such table'
                _ready.add(db_path)
    conn.row_factory = sqlite3.Row
    return conn
