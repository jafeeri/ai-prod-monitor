"""CLI:  python -m monitor <command>

    watch [db]           one monitoring tick: sample+judge new traffic, check drift, raise alerts
                         (this is the unit Claude's /loop repeats for continuous alerting)
    show  <trace_id>     drill into one trace: span tree + tokens/latency/cost + prompt/response
    report [db]          cost rollup + eval summary + current drift status
"""
from __future__ import annotations

import sys

from . import cost as cost_mod
from .alerts import raise_alerts, recent_alerts
from .drift import Thresholds, check_drift
from .eval import run_online_eval, summary
from .tracer import Tracer

DB = "monitor.db"


def _find_trace(db_path: str, trace_id: str) -> None:
    tr = Tracer(db_path)
    spans = tr.get_spans(trace_id)
    if not spans:
        print(f"no trace {trace_id} in {db_path}")
        return
    print(f"trace {trace_id}  ({len(spans)} spans)")
    by_id = {s["span_id"]: s for s in spans}
    depth = {s["span_id"]: (1 if s["parent_id"] in by_id else 0) for s in spans}
    for s in spans:
        pad = "  " * depth[s["span_id"]]
        extra = ""
        if s["kind"] == "llm":
            a = s["attributes"]
            extra = (f"  [{a.get('gen_ai.request.model')}  "
                     f"{a.get('gen_ai.usage.input_tokens')}in/{a.get('gen_ai.usage.output_tokens')}out  "
                     f"${a.get('gen_ai.usage.cost_usd', 0):.6f}]")
        print(f"{pad}- {s['name']} ({s['kind']}, {s['latency_ms']:.1f}ms){extra}")
        for e in tr.get_events(s["span_id"]):
            text = str(e["attributes"].get("text", ""))[:120]
            print(f"{pad}    {e['name']}: {text}")


def _watch(db_path: str) -> None:
    scored = run_online_eval(db_path)
    alerts = raise_alerts(db_path)
    if not alerts:
        print(f"[ok] watched: scored {scored} new sample(s), no new alerts")


def _report(db_path: str) -> None:
    print("== cost ==");  print("  totals:", cost_mod.totals(db_path))
    for r in cost_mod.by_model(db_path):
        print("  model :", r)
    print("== quality ==");  print("  ", summary(db_path))
    print("== drift ==")
    for f in check_drift(db_path):
        flag = "BREACH" if f["breached"] else "ok"
        print(f"  [{flag}] {f['message']}")
    al = recent_alerts(db_path, 5)
    if al:
        print("== recent alerts ==")
        for a in al:
            print(f"  {a['metric']}: {a['message']} -> {a['trace_id']}")


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "watch":
        _watch(rest[0] if rest else DB)
    elif cmd == "show":
        if not rest:
            print("usage: python -m monitor show <trace_id> [db]"); return 2
        _find_trace(rest[1] if len(rest) > 1 else DB, rest[0])
    elif cmd == "report":
        _report(rest[0] if rest else DB)
    else:
        print(__doc__); return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
