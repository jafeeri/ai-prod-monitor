"""Drift & anomaly detection: recent window vs rolling baseline.

A single bad request is noise; a sustained move is a signal. We compare a recent
window of each metric against the baseline window just before it, and flag when
the move crosses a threshold:

    quality (avg judge overall)  → flag a DROP    (recent - baseline <= -quality_drop)
    latency (avg llm latency_ms) → flag a RISE    (recent/baseline - 1 >= latency_increase)
    cost    (avg $/call)         → flag a SPIKE   (recent/baseline - 1 >= cost_increase)

Returns a finding per metric (breached or not) so the dashboard can show status;
P5 alerting fires on the breached ones and drill-links to the offending traces.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import store


@dataclass
class Thresholds:
    quality_drop: float = 1.0       # points on the 1-5 scale
    latency_increase: float = 0.5   # relative, +50%
    cost_increase: float = 0.5      # relative, +50%
    window: int = 10                # samples per window


def _series(db_path: str, sql: str) -> list[float]:
    db = store.connect(db_path)  # ensures schema → safe on a fresh db
    try:
        return [r[0] for r in db.execute(sql).fetchall() if r[0] is not None]
    finally:
        db.close()


def _split(series: list[float], window: int):
    """recent = last `window`; baseline = the window just before it (fair, same-size).
    Falls back to all-prior when there isn't a full baseline window yet."""
    recent = series[-window:]
    baseline = series[-2 * window:-window] or series[:-window]
    return baseline, recent


def _avg(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def _finding(metric: str, baseline, recent, breached, delta, direction, msg) -> dict:
    return {"metric": metric, "baseline": baseline, "recent": recent,
            "breached": breached, "delta": delta, "direction": direction, "message": msg}


def check_drift(db_path: str, thr: Thresholds = Thresholds()) -> list[dict]:
    w = thr.window
    out: list[dict] = []

    q = _series(db_path, "SELECT overall FROM scores WHERE overall IS NOT NULL ORDER BY ts_ns")
    base, rec = _split(q, w)
    if base and rec:
        b, r = _avg(base), _avg(rec)
        drop = b - r
        breached = drop >= thr.quality_drop
        out.append(_finding("quality", round(b, 2), round(r, 2), breached, round(-drop, 2),
                            "down", f"quality {b:.2f}->{r:.2f} (drop {drop:.2f}, thr {thr.quality_drop})"))

    for metric, sql, thresh in (
        ("latency", "SELECT latency_ms FROM spans WHERE kind='llm' ORDER BY start_ns", thr.latency_increase),
        ("cost", "SELECT cost_usd FROM spans WHERE kind='llm' AND cost_usd IS NOT NULL ORDER BY start_ns", thr.cost_increase),
    ):
        s = _series(db_path, sql)
        base, rec = _split(s, w)
        if not (base and rec):
            continue
        b, r = _avg(base), _avg(rec)
        rel = (r / b - 1) if b else 0.0
        breached = rel >= thresh
        unit = "ms" if metric == "latency" else "$"
        out.append(_finding(metric, round(b, 6), round(r, 6), breached, round(rel, 2),
                            "up", f"{metric} {b:.4g}->{r:.4g}{unit} (+{rel*100:.0f}%, thr +{thresh*100:.0f}%)"))
    return out


def _selfcheck() -> None:
    import os
    import tempfile

    path = os.path.join(tempfile.mkdtemp(), "drift.db")
    db = store.connect(path)  # ensures full schema
    # baseline window: healthy quality ~4.5, then recent window: degraded ~2.5
    for i, ov in enumerate([4.5] * 10 + [2.5] * 10):
        db.execute("INSERT INTO scores(span_id,ts_ns,overall) VALUES(?,?,?)",
                   (f"s{i}", i, ov))
    # baseline latency ~100ms then recent ~300ms (a +200% rise)
    for i, lat in enumerate([100.0] * 10 + [300.0] * 10):
        db.execute(
            "INSERT INTO spans(span_id,trace_id,parent_id,name,kind,start_ns,end_ns,"
            "latency_ms,model,tokens_in,tokens_out,cost_usd,attributes) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"sp{i}", "t", None, "llm_call", "llm", i, i, lat, "m", 10, 10, 0.001, "{}"))
    db.commit()
    db.close()

    findings = {f["metric"]: f for f in check_drift(path, Thresholds(window=10))}
    assert findings["quality"]["breached"], findings["quality"]
    assert findings["latency"]["breached"], findings["latency"]
    assert not findings["cost"]["breached"], findings["cost"]  # flat cost → no spike
    print("drift self-check OK:",
          f"quality {findings['quality']['baseline']}->{findings['quality']['recent']} BREACH,",
          f"latency +{int(findings['latency']['delta']*100)}% BREACH, cost steady")


if __name__ == "__main__":
    _selfcheck()
