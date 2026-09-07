"""The SDK: wrap ANY LLM app in ~2 lines.

    mon = Monitor("monitor.db")
    with mon.trace("chat_request"):
        with mon.step("retrieve", kind="retrieval"):
            ctx = my_retriever(q)
        answer = mon.chat(build_prompt(ctx, q))["text"]

- mon.trace(name)        one request (context manager)
- mon.step(name, kind)   any sub-step as a span (context manager)
- mon.chat(prompt)       an instrumented LLM call: routes through the provider
                         client, records tokens/latency/model, prompt+response as events
- mon.wrap(fn)           decorator: time any callable as a span
"""
from __future__ import annotations

import functools
from contextlib import contextmanager

from .cost import cost_usd
from .llm import complete
from .tracer import Tracer, _current_trace


class Monitor:
    def __init__(self, db_path: str = "monitor.db"):
        self.tracer = Tracer(db_path)

    def trace(self, name: str, **meta):
        return self.tracer.trace(name, **meta)

    @contextmanager
    def _auto_trace(self, name: str):
        # A top-level chat/step/wrap is its own trace: open one if none is active,
        # otherwise attach to the caller's trace. So mon.chat(...) works standalone.
        if _current_trace.get() is None:
            with self.trace(name):
                yield
        else:
            yield

    @contextmanager
    def step(self, name: str, kind: str = "tool"):
        """Any sub-step as a span. Auto-opens a trace if called outside one."""
        with self._auto_trace(name), self.tracer.span(name, kind) as s:
            yield s

    def chat(self, prompt: str, model: str | None = None) -> dict:
        """Instrumented LLM call. Returns the provider dict (text + usage)."""
        with self._auto_trace("chat"), self.tracer.span("llm_call", kind="llm") as s:
            r = complete(prompt, model=model)
            s.set_llm(r["model"], r["tokens_in"], r["tokens_out"], system=r["system"])
            s.set("gen_ai.usage.cost_usd", cost_usd(r["model"], r["tokens_in"], r["tokens_out"]))
            s.event("gen_ai.content.prompt", text=prompt)
            s.event("gen_ai.content.response", text=r["text"])
            return r

    def wrap(self, fn=None, *, name: str | None = None, kind: str = "tool"):
        """Decorator: run fn inside a span. @mon.wrap or @mon.wrap(kind='retrieval')."""
        def deco(f):
            @functools.wraps(f)
            def inner(*args, **kwargs):
                with self._auto_trace(name or f.__name__), \
                        self.tracer.span(name or f.__name__, kind):
                    return f(*args, **kwargs)
            return inner
        return deco(fn) if callable(fn) else deco


def _selfcheck() -> None:
    import os
    import tempfile

    os.environ.setdefault("MONITOR_LLM", "mock")
    path = os.path.join(tempfile.mkdtemp(), "sdk.db")
    mon = Monitor(path)

    @mon.wrap(kind="retrieval")
    def retrieve(q):
        return ["a span is a step in a trace"]

    with mon.trace("chat_request") as tid:
        ctx = retrieve("what is a span?")
        r = mon.chat(f"Context: {ctx}\nQ: what is a span?")

    spans = mon.tracer.get_spans(tid)
    kinds = {s["name"]: s["kind"] for s in spans}
    assert kinds.get("retrieve") == "retrieval", kinds
    assert kinds.get("llm_call") == "llm", kinds

    llm = next(s for s in spans if s["kind"] == "llm")
    assert llm["attributes"]["gen_ai.usage.input_tokens"] > 0
    assert llm["attributes"]["gen_ai.usage.output_tokens"] > 0
    ev = {e["name"] for e in mon.tracer.get_events(llm["span_id"])}
    assert ev == {"gen_ai.content.prompt", "gen_ai.content.response"}, ev
    assert r["text"], "empty completion"

    # standalone chat outside any trace() must work (auto-opens its own trace)
    r2 = mon.chat("standalone call, no explicit trace")
    assert r2["text"], "standalone chat failed"
    with mon.step("solo", kind="tool"):  # standalone step too
        pass
    print(f"sdk self-check OK: {len(spans)} spans, standalone chat+step ok, "
          f"llm={llm['attributes']['gen_ai.request.model']}")


if __name__ == "__main__":
    _selfcheck()
