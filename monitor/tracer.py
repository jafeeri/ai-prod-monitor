"""Trace/span core for AI Prod Monitor.

A *trace* is one request. A *span* is one step inside it (an LLM call, a
retrieval, a tool call), forming a tree via parent links. LLM spans carry
token counts + model; the prompt/response ride as span EVENTS, not attributes,
so they stay droppable and never bloat a span (cardinality control).

Zero dependency: stdlib sqlite3 + contextvars. Attribute names follow the
OpenTelemetry GenAI semantic conventions (gen_ai.*) so an OTLP exporter is a
thin adapter later, not a rewrite.

Concurrency: the current span AND the current trace live in ContextVars (correct
across threads and async), and each thread gets its own DB connection (WAL) — so
concurrent requests don't collide or misattribute spans.
"""
from __future__ import annotations

import contextvars
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field

from . import store

# The current span and current trace for this logical thread of execution.
# ContextVars (not instance attributes) so concurrent requests don't clobber
# each other, and so a span opened outside any trace correctly finds none.
_current: contextvars.ContextVar["Span | None"] = contextvars.ContextVar(
    "current_span", default=None)
_current_trace: contextvars.ContextVar["str | None"] = contextvars.ContextVar(
    "current_trace", default=None)

# Cap oversized event payloads (prompt/response) so one giant paste can't bloat
# the store. 0 = unlimited. Override with MONITOR_MAX_EVENT_CHARS.
_MAX_EVENT_CHARS = int(os.environ.get("MONITOR_MAX_EVENT_CHARS", "16384"))


def _id() -> str:
    return uuid.uuid4().hex[:16]


def _cap(value):
    if _MAX_EVENT_CHARS and isinstance(value, str) and len(value) > _MAX_EVENT_CHARS:
        return value[:_MAX_EVENT_CHARS] + f"...[+{len(value) - _MAX_EVENT_CHARS} chars]"
    return value


@dataclass
class Event:
    name: str
    ts_ns: int
    attributes: dict


@dataclass
class Span:
    span_id: str
    trace_id: str
    parent_id: str | None
    name: str
    kind: str  # llm | retrieval | tool | other
    start_ns: int
    _perf_start: int
    end_ns: int | None = None
    latency_ms: float | None = None
    attributes: dict = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)

    def set(self, key: str, value) -> "Span":
        """Set one attribute. Use gen_ai.* keys for OTel compatibility."""
        self.attributes[key] = value
        return self

    def set_llm(self, model: str, tokens_in: int, tokens_out: int,
                system: str | None = None) -> "Span":
        """Convenience for the fields every LLM span needs, OTel-named."""
        self.set("gen_ai.request.model", model)
        self.set("gen_ai.usage.input_tokens", int(tokens_in))
        self.set("gen_ai.usage.output_tokens", int(tokens_out))
        if system:
            self.set("gen_ai.system", system)
        return self

    def event(self, name: str, **attributes) -> "Span":
        """Attach a span event (e.g. the prompt/response) — droppable payload."""
        capped = {k: _cap(v) for k, v in attributes.items()}
        self.events.append(Event(name, time.time_ns(), capped))
        return self


class Tracer:
    """Owns the store and hands out traces/spans.

    Usage:
        tr = Tracer("monitor.db")
        with tr.trace("chat") as t:
            with tr.span("llm_call", kind="llm") as s:
                s.set_llm("claude-sonnet-5", 812, 140, system="anthropic")
                s.event("gen_ai.content.prompt", text=prompt)
                s.event("gen_ai.content.response", text=out)
    """

    def __init__(self, db_path: str = "monitor.db"):
        self.db_path = db_path
        self._local = threading.local()
        store.connect(db_path).close()  # ensure schema + WAL once up front

    def _conn(self):
        # One connection per thread — avoids 'transaction within a transaction'
        # from sharing a single connection across concurrent requests.
        c = getattr(self._local, "conn", None)
        if c is None:
            c = store.connect(self.db_path)
            self._local.conn = c
        return c

    # ---- traces -----------------------------------------------------------
    @contextmanager
    def trace(self, name: str, **meta):
        trace_id = _id()
        start_ns = time.time_ns()
        db = self._conn()
        db.execute("INSERT INTO traces(trace_id,name,start_ns,end_ns,meta) VALUES(?,?,?,?,?)",
                   (trace_id, name, start_ns, None, json.dumps(meta)))
        db.commit()
        tok_span = _current.set(None)            # a new trace starts with no parent span
        tok_trace = _current_trace.set(trace_id)
        try:
            yield trace_id
        finally:
            db = self._conn()
            db.execute("UPDATE traces SET end_ns=? WHERE trace_id=?",
                       (time.time_ns(), trace_id))
            db.commit()
            _current_trace.reset(tok_trace)
            _current.reset(tok_span)

    # ---- spans ------------------------------------------------------------
    @contextmanager
    def span(self, name: str, kind: str = "other"):
        parent = _current.get()
        trace_id = parent.trace_id if parent else _current_trace.get()
        if trace_id is None:
            raise RuntimeError("span() called outside a trace()")
        s = Span(span_id=_id(), trace_id=trace_id,
                 parent_id=parent.span_id if parent else None,
                 name=name, kind=kind, start_ns=time.time_ns(),
                 _perf_start=time.perf_counter_ns())
        token = _current.set(s)
        try:
            yield s
        except BaseException as e:  # capture the failure ON the span, then re-raise
            s.set("otel.status_code", "ERROR")
            s.set("error.type", type(e).__name__)
            s.set("error.message", str(e)[:500])
            s.event("exception", type=type(e).__name__, message=str(e)[:500])
            raise
        finally:
            s.end_ns = time.time_ns()
            s.latency_ms = (time.perf_counter_ns() - s._perf_start) / 1e6
            self._persist_span(s)
            _current.reset(token)

    def _persist_span(self, s: Span) -> None:
        # Project the metric-bearing attributes into real columns so cost/token
        # rollups are plain SQL (SUM/percentile) instead of JSON surgery.
        a = s.attributes
        db = self._conn()
        db.execute(
            "INSERT INTO spans(span_id,trace_id,parent_id,name,kind,start_ns,end_ns,"
            "latency_ms,model,tokens_in,tokens_out,cost_usd,attributes) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (s.span_id, s.trace_id, s.parent_id, s.name, s.kind, s.start_ns,
             s.end_ns, s.latency_ms, a.get("gen_ai.request.model"),
             a.get("gen_ai.usage.input_tokens"), a.get("gen_ai.usage.output_tokens"),
             a.get("gen_ai.usage.cost_usd"), json.dumps(a)))
        if s.events:
            db.executemany(
                "INSERT INTO events(span_id,trace_id,name,ts_ns,attributes) VALUES(?,?,?,?,?)",
                [(s.span_id, s.trace_id, e.name, e.ts_ns, json.dumps(e.attributes))
                 for e in s.events])
        db.commit()

    # ---- read back --------------------------------------------------------
    def get_spans(self, trace_id: str) -> list[dict]:
        db = store.connect(self.db_path)
        try:
            cur = db.execute(
                "SELECT span_id,parent_id,name,kind,latency_ms,attributes "
                "FROM spans WHERE trace_id=? ORDER BY start_ns", (trace_id,))
            return [{"span_id": r[0], "parent_id": r[1], "name": r[2], "kind": r[3],
                     "latency_ms": r[4], "attributes": json.loads(r[5])}
                    for r in cur.fetchall()]
        finally:
            db.close()

    def get_events(self, span_id: str) -> list[dict]:
        db = store.connect(self.db_path)
        try:
            cur = db.execute(
                "SELECT name,ts_ns,attributes FROM events WHERE span_id=? ORDER BY ts_ns",
                (span_id,))
            return [{"name": r[0], "ts_ns": r[1], "attributes": json.loads(r[2])}
                    for r in cur.fetchall()]
        finally:
            db.close()


def _selfcheck() -> None:
    """Trace a fake request; assert the span tree, tokens, latency, events round-trip."""
    import tempfile

    path = os.path.join(tempfile.mkdtemp(), "selfcheck.db")
    tr = Tracer(path)

    with tr.trace("chat_request", user="demo") as tid:
        with tr.span("handle", kind="other"):
            with tr.span("retrieve", kind="retrieval") as rs:
                rs.set("gen_ai.retrieval.k", 3)
                time.sleep(0.005)
            with tr.span("llm_call", kind="llm") as ls:
                ls.set_llm("claude-sonnet-5", 812, 140, system="anthropic")
                ls.event("gen_ai.content.prompt", text="What is a span?")
                ls.event("gen_ai.content.response", text="A step in a trace.")
                time.sleep(0.005)

    spans = tr.get_spans(tid)
    assert len(spans) == 3, f"expected 3 spans, got {len(spans)}"
    by_name = {s["name"]: s for s in spans}
    handle = by_name["handle"]
    assert handle["parent_id"] is None, "handle should be a root span"
    assert by_name["retrieve"]["parent_id"] == handle["span_id"]
    assert by_name["llm_call"]["parent_id"] == handle["span_id"]
    llm = by_name["llm_call"]
    assert llm["attributes"]["gen_ai.usage.input_tokens"] == 812
    assert llm["attributes"]["gen_ai.usage.output_tokens"] == 140
    assert llm["latency_ms"] > 0, "latency not recorded"
    assert "gen_ai.content.prompt" not in llm["attributes"], "prompt leaked into attributes"
    events = tr.get_events(llm["span_id"])
    assert {e["name"] for e in events} == {"gen_ai.content.prompt", "gen_ai.content.response"}

    # exception capture: a raising span records an error status + exception event
    try:
        with tr.trace("boom") as btid:
            with tr.span("bad", kind="tool"):
                raise ValueError("nope")
    except ValueError:
        pass
    bad = tr.get_spans(btid)[0]
    assert bad["attributes"].get("otel.status_code") == "ERROR", "error not captured on span"
    assert any(e["name"] == "exception" for e in tr.get_events(bad["span_id"]))

    print("tracer self-check OK:",
          f"{len(spans)} spans, llm latency {llm['latency_ms']:.2f}ms, error-capture ok")


if __name__ == "__main__":
    _selfcheck()
