"""Live dashboard for AI Prod Monitor.

Reads the same SQLite the SDK writes — fully decoupled from the monitored app.

    streamlit run dashboard.py                 # watches monitor.db
    streamlit run dashboard.py -- --db other.db --watch 5

--watch N auto-refreshes every N seconds (the live view).
"""
from __future__ import annotations

import argparse
import sqlite3

import streamlit as st

from monitor import cost as cost_mod
from monitor.alerts import recent_alerts
from monitor.drift import check_drift
from monitor.eval import summary


def _args():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="monitor.db")
    p.add_argument("--watch", type=int, default=0, help="auto-refresh seconds (0=off)")
    return p.parse_known_args()[0]


def _rows(db, sql, args=()):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    except sqlite3.OperationalError:
        return []
    finally:
        c.close()


def main():
    a = _args()
    st.set_page_config(page_title="AI Prod Monitor", layout="wide")
    if a.watch > 0:
        st.markdown(
            f"<meta http-equiv='refresh' content='{a.watch}'>", unsafe_allow_html=True)

    st.title("AI Prod Monitor")
    st.caption(f"db: {a.db}" + (f"  ·  auto-refresh {a.watch}s" if a.watch else ""))

    tot = cost_mod.totals(a.db) or {}
    q = summary(a.db) or {}
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Traces", tot.get("traces") or 0)
    c2.metric("LLM calls", tot.get("llm_calls") or 0)
    c3.metric("Tokens", (tot.get("tokens_in") or 0) + (tot.get("tokens_out") or 0))
    c4.metric("Cost (USD)", f"${tot.get('cost_usd') or 0:.4f}")
    c5.metric("Quality", q.get("overall") if q.get("n") else "—")

    # --- alerts + drift status -------------------------------------------
    st.subheader("Drift & alerts")
    findings = check_drift(a.db)
    if findings:
        cols = st.columns(len(findings))
        for col, f in zip(cols, findings):
            state = "BREACH" if f["breached"] else "ok"
            (col.error if f["breached"] else col.success)(f"{f['metric']}: {state}\n\n{f['message']}")
    else:
        st.info("Not enough history yet for a baseline vs recent window.")

    al = recent_alerts(a.db, 20)
    if al:
        st.write("**Recent alerts**")
        st.dataframe([{"metric": x["metric"], "message": x["message"],
                       "trace": x["trace_id"]} for x in al], width='stretch')

    # --- breakdowns -------------------------------------------------------
    left, right = st.columns(2)
    with left:
        st.subheader("Cost by model")
        bm = cost_mod.by_model(a.db)
        if bm:
            st.dataframe(bm, width='stretch')
        else:
            st.write("—")
        st.subheader("Latency (recent LLM spans, ms)")
        lat = _rows(a.db, "SELECT latency_ms FROM spans WHERE kind='llm' "
                          "ORDER BY start_ns DESC LIMIT 100")
        if lat:
            st.line_chart([r["latency_ms"] for r in reversed(lat)])
        else:
            st.write("—")
    with right:
        st.subheader("Quality over time (judged samples)")
        sc = _rows(a.db, "SELECT overall FROM scores WHERE overall IS NOT NULL "
                         "ORDER BY ts_ns DESC LIMIT 100")
        if sc:
            st.line_chart([r["overall"] for r in reversed(sc)])
        else:
            st.write("—")
        st.subheader("Cost per trace")
        st.dataframe(cost_mod.by_trace(a.db), width='stretch')

    # --- recent traces ----------------------------------------------------
    st.subheader("Recent traces")
    tr = _rows(a.db, "SELECT trace_id, name, start_ns FROM traces ORDER BY start_ns DESC LIMIT 25")
    if tr:
        st.dataframe(tr, width='stretch')
    else:
        st.write("—")


if __name__ == "__main__":
    main()
