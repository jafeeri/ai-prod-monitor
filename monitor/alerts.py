"""Alerting: turn breached drift findings into alerts you can act on.

Each alert carries the offending trace_id — the whole point of observability is
that an alarm drills straight into the exact run that caused it. A per-metric
cooldown stops the /loop watch from re-firing the same ongoing drift every tick.
"""
from __future__ import annotations

import time

from . import store
from .drift import Thresholds, check_drift

# Which trace best explains each breached metric (worst recent example).
_OFFENDER = {
    "quality": "SELECT trace_id FROM scores WHERE overall IS NOT NULL "
               "ORDER BY overall ASC, ts_ns DESC LIMIT 1",
    "latency": "SELECT trace_id FROM spans WHERE kind='llm' ORDER BY latency_ms DESC LIMIT 1",
    "cost":    "SELECT trace_id FROM spans WHERE kind='llm' AND cost_usd IS NOT NULL "
               "ORDER BY cost_usd DESC LIMIT 1",
}


def raise_alerts(db_path: str, thr: Thresholds = Thresholds(),
                 cooldown_s: float = 300.0) -> list[dict]:
    """Check drift; for each breached metric (past cooldown) insert + print an alert."""
    db = store.connect(db_path)
    now = time.time_ns()
    cooldown_ns = cooldown_s * 1e9
    new: list[dict] = []
    for f in check_drift(db_path, thr):
        if not f["breached"]:
            continue
        last = db.execute("SELECT MAX(ts_ns) FROM alerts WHERE metric=?",
                          (f["metric"],)).fetchone()[0]
        if last and now - last < cooldown_ns:
            continue  # already alerted on this metric recently
        row = db.execute(_OFFENDER[f["metric"]]).fetchone()
        trace_id = row[0] if row else None
        db.execute("INSERT INTO alerts(ts_ns,metric,message,trace_id) VALUES(?,?,?,?)",
                   (now, f["metric"], f["message"], trace_id))
        print(f"[ALERT] {f['message']}  -> trace: {trace_id}  (monitor show {trace_id})")
        new.append({"metric": f["metric"], "message": f["message"], "trace_id": trace_id})
    db.commit()
    db.close()
    return new


def recent_alerts(db_path: str, limit: int = 20) -> list[dict]:
    db = store.connect(db_path)
    rows = db.execute("SELECT ts_ns,metric,message,trace_id FROM alerts "
                      "ORDER BY ts_ns DESC LIMIT ?", (limit,)).fetchall()
    db.close()
    return [dict(r) for r in rows]


def _selfcheck() -> None:
    import os
    import tempfile

    path = os.path.join(tempfile.mkdtemp(), "alerts.db")
    db = store.connect(path)
    for i, ov in enumerate([4.5] * 10 + [2.0] * 10):
        db.execute("INSERT INTO scores(trace_id,span_id,ts_ns,overall) VALUES(?,?,?,?)",
                   (f"tr{i}", f"s{i}", i, ov))
    db.commit()
    db.close()

    new = raise_alerts(path, Thresholds(window=10), cooldown_s=300)
    assert any(a["metric"] == "quality" for a in new), new
    q = next(a for a in new if a["metric"] == "quality")
    assert q["trace_id"] == "tr19", q  # worst recent score -> its trace

    # cooldown: immediate re-run raises nothing new
    assert raise_alerts(path, Thresholds(window=10), cooldown_s=300) == [], "cooldown failed"
    assert len(recent_alerts(path)) >= 1
    print(f"alerts self-check OK: raised {len(new)} (quality->{q['trace_id']}), cooldown held")


if __name__ == "__main__":
    _selfcheck()
