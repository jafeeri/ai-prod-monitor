"""Adversarial / stress test for AI Prod Monitor.

Runs the failure-path scenarios from the testing guide and prints PASS / BUG per
scenario, plus a totals line. Offline (mock backend), no API key.

    python tests/adversarial_test.py

This is a DIAGNOSTIC harness, not a green regression suite — today it reports the
known bugs (concurrency, fresh-db crashes, provider-outage handling, span error
capture). As fixes land, BUG lines should turn into PASS lines.
"""
import os, sys, json, sqlite3, tempfile, threading

os.environ["MONITOR_LLM"] = "mock"
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)

from monitor.sdk import Monitor
from monitor import cost, eval as ev, drift, alerts

def tmp(name): return os.path.join(tempfile.mkdtemp(), name)
def line(t): print("\n" + "=" * 70 + f"\n{t}\n" + "=" * 70)

results = {"PASS": 0, "BUG": 0}
def rep(tag, msg): results[tag] = results.get(tag, 0) + 1; print(f"  [{tag}] {msg}")


def s_fresh_db():
    line("B. Fresh/nonexistent DB (missing tables) - read paths")
    p = tmp("empty.db")
    for name, fn in [("cost.totals", lambda: cost.totals(p)),
                     ("cost.by_model", lambda: cost.by_model(p)),
                     ("eval.summary", lambda: ev.summary(p)),
                     ("drift.check_drift", lambda: drift.check_drift(p)),
                     ("alerts.raise_alerts", lambda: alerts.raise_alerts(p))]:
        try:
            fn(); rep("PASS", f"{name} on fresh db ok")
        except Exception as e:
            rep("BUG", f"{name} crashes on fresh db: {type(e).__name__}: {e}")


def s_concurrency():
    line("C. Concurrency - 20 threads x 10 traces on ONE shared Monitor")
    p = tmp("conc.db"); mon = Monitor(p); errs = []
    def worker(wid):
        try:
            for i in range(10):
                with mon.trace(f"w{wid}"):
                    with mon.step("retrieve", kind="retrieval"): pass
                    mon.chat(f"worker {wid} req {i}")
        except Exception as e:
            errs.append(f"{type(e).__name__}: {e}")
    ths = [threading.Thread(target=worker, args=(w,)) for w in range(20)]
    [t.start() for t in ths]; [t.join() for t in ths]
    rep("BUG" if errs else "PASS",
        f"{len(errs)} thread exceptions" + (f", e.g. {errs[0]}" if errs else ""))
    db = sqlite3.connect(p)
    ntr = db.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
    llm = db.execute("SELECT COUNT(*) FROM spans WHERE kind='llm'").fetchone()[0]
    bad = db.execute("SELECT COUNT(*) FROM spans WHERE trace_id NOT IN "
                     "(SELECT trace_id FROM traces)").fetchone()[0]
    db.close()
    print(f"    traces={ntr}/200  llm={llm}/200  orphan_spans={bad}")
    rep("PASS" if ntr == 200 and llm == 200 else "BUG",
        f"write integrity: traces {ntr}/200, llm {llm}/200")
    if bad:
        rep("BUG", f"{bad} spans on a nonexistent trace_id (race on _active_trace)")


def s_active_trace():
    line("C2. _active_trace race - span after trace ends (sequential)")
    p = tmp("seq.db"); m = Monitor(p)
    with m.trace("A") as tA:
        m.chat("in A")
    try:
        with m.tracer.span("orphan", kind="tool"):
            pass
        db = sqlite3.connect(p)
        row = db.execute("SELECT trace_id FROM spans WHERE name='orphan'").fetchone()
        db.close()
        if row and row[0] == tA:
            rep("BUG", f"span() outside a trace silently attached to previous trace {tA}")
        else:
            rep("PASS", "orphan span handled")
    except RuntimeError as e:
        rep("PASS", f"span() outside trace correctly raised: {e}")


def s_provider_outage():
    line("D. Provider outage - the LLM call raises")
    # Simulate the outage deterministically (don't depend on whether a real ollama
    # is up). Correct behavior: the app still gets its exception (monitor doesn't
    # hide it), but the failure is RECORDED on the span so it's visible in the trace.
    import urllib.error
    import monitor.sdk as sdk
    saved = sdk.complete
    def boom(prompt, model=None):
        raise urllib.error.URLError("connection refused")
    sdk.complete = boom
    p = tmp("prov.db"); m = Monitor(p)
    tid = None
    try:
        with m.trace("t") as tid:
            m.chat("hello")
    except Exception:
        pass
    finally:
        sdk.complete = saved
    spans = m.tracer.get_spans(tid) if tid else []
    errspan = next((s for s in spans if s["attributes"].get("otel.status_code") == "ERROR"), None)
    if errspan:
        rep("PASS", f"provider outage recorded on span: {errspan['attributes'].get('error.type')}")
    else:
        rep("BUG", "provider outage not captured on any span")


def s_span_exception():
    line("E. Exception inside a wrapped step - captured? trace closes?")
    p = tmp("exc.db"); m = Monitor(p)
    try:
        with m.trace("t"):
            with m.tracer.span("boom", kind="tool"):
                raise ValueError("kaboom")
    except ValueError:
        pass
    db = sqlite3.connect(p)
    sp = db.execute("SELECT attributes FROM spans WHERE name='boom'").fetchone()
    tr = db.execute("SELECT end_ns FROM traces").fetchone()
    db.close()
    if sp is None:
        rep("BUG", "span that raised was NOT persisted")
    else:
        attrs = json.loads(sp[0])
        if any(k in attrs for k in ("error", "exception", "otel.status_code", "gen_ai.error")):
            rep("PASS", "exception captured on span")
        else:
            rep("BUG", "span persisted but exception NOT recorded (no error/status attribute)")
    rep("PASS" if tr and tr[0] is not None else "BUG", "trace closed after inner exception")


def s_fuzz():
    line("F. Fuzz - empty / huge / unicode / control / json prompts")
    p = tmp("fuzz.db"); m = Monitor(p)
    cases = {"empty": "", "huge": "x " * 500000, "unicode": "日本語 \U0001f389",
             "controls": "a\x00b\x01c\r\n\t", "jsonish": '{"role":"user"}',
             "nullbyte": "\x00", "long_word": "z" * 200000}
    for name, txt in cases.items():
        try:
            with m.trace("fuzz"):
                m.chat(txt)
            rep("PASS", f"fuzz[{name}] len={len(txt)} ok")
        except Exception as e:
            rep("BUG", f"fuzz[{name}] crashed: {type(e).__name__}: {e}")


def s_judge_parse():
    line("G. Judge JSON parse - junk / prose-braces / nested / wrong-types")
    saved = ev.complete
    def fake(txt, model=None):
        return {"text": fake.out, "model": "judge-x", "tokens_in": 1, "tokens_out": 1, "system": "x"}
    ev.complete = fake
    os.environ["MONITOR_LLM"] = "anthropic"
    for name, out in [("junk", "no json here"),
                      ("prose_braces", "I think {the answer} is {5}"),
                      ("valid", '{"reason":"ok","relevance":4,"faithfulness":4,"safety":5}'),
                      ("nested", 'x {"reason":"y","relevance":3,"faithfulness":3,"safety":3} {oops}'),
                      ("wrong_types", '{"relevance":"high","faithfulness":null,"safety":5}')]:
        fake.out = out
        try:
            r = ev.judge("q", "a")
            rep("PASS", f"judge[{name}] -> rel={r.get('relevance')}")
        except Exception as e:
            rep("BUG", f"judge[{name}] crashed: {type(e).__name__}: {e}")
    ev.complete = saved
    os.environ["MONITOR_LLM"] = "mock"


def s_live_readwrite():
    line("H. Live dashboard - read while writing (SQLite locking / WAL)")
    p = tmp("live.db"); m = Monitor(p); stop = [False]; rerr = []; werr = []
    def writer():
        try:
            for i in range(200):
                with m.trace("t"): m.chat(f"req {i}")
        except Exception as e:
            werr.append(f"{type(e).__name__}: {e}")
    def reader():
        while not stop[0]:
            try:
                cost.totals(p); drift.check_drift(p)
            except Exception as e:
                rerr.append(f"{type(e).__name__}: {e}")
    wt = threading.Thread(target=writer); rt = threading.Thread(target=reader)
    rt.start(); wt.start(); wt.join(); stop[0] = True; rt.join()
    rep("BUG" if werr else "PASS", "writer" + (f" errors: {werr[0]}" if werr else " survived reads"))
    rep("BUG" if rerr else "PASS",
        "reader" + (f" errors: {rerr[0]} (x{len(rerr)})" if rerr else " survived writes"))


def s_cost_edges():
    line("I. Cost edge cases")
    for name, got, exp in [("unknown model", cost.cost_usd("gpt-99", 100, 100), 0.0),
                           ("None tokens", cost.cost_usd("claude-sonnet-5", None, None), 0.0)]:
        rep("PASS" if got == exp else "BUG", f"{name}: got {got} exp {exp}")
    neg = cost.cost_usd("claude-sonnet-5", -1000, -1000)
    rep("BUG" if neg < 0 else "PASS", f"negative tokens -> ${neg}")


def s_sampling():
    line("L. Sampling extremes (0, 1, 2, -1)")
    p = tmp("samp.db"); m = Monitor(p)
    for i in range(30):
        with m.trace("t"): m.chat(f"q{i}")
    for rate in [0.0, 1.0, 2.0, -1.0]:
        c = sqlite3.connect(p); c.execute("DELETE FROM scores"); c.commit(); c.close()
        try:
            n = ev.run_online_eval(p, sample_rate=rate)
            rep("PASS", f"rate={rate} -> scored {n}")
        except Exception as e:
            rep("BUG", f"rate={rate} crashed: {type(e).__name__}: {e}")


if __name__ == "__main__":
    for s in (s_fresh_db, s_concurrency, s_active_trace, s_provider_outage,
              s_span_exception, s_fuzz, s_judge_parse, s_live_readwrite,
              s_cost_edges, s_sampling):
        s()
    print("\n" + "#" * 70 + f"\n# TOTALS: {results}\n" + "#" * 70)
