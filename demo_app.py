"""A generic, self-contained LLM app for the monitor to watch.

A tiny FAQ assistant: retrieve the most relevant doc by word overlap, then ask
the LLM to answer using it. Instrumented with the monitor SDK — nothing else
here knows or cares that it's being observed. Run it and it emits real traces.

    python demo_app.py            # answer the built-in questions
    python demo_app.py "your question here"
"""
from __future__ import annotations

import sys

from monitor.sdk import Monitor

DOCS = [
    ("refunds", "Refunds are issued within 5 business days to the original payment method."),
    ("hours", "Support is available Monday to Friday, 9am to 6pm local time."),
    ("reset", "To reset your password, use the 'Forgot password' link on the sign-in page."),
    ("plans", "The Free plan covers one project; paid plans add unlimited projects and priority support."),
    ("export", "You can export your data anytime as CSV or JSON from Settings > Data."),
]

QUESTIONS = [
    "How long do refunds take?",
    "When is support open?",
    "How do I reset my password?",
    "Can I export my data?",
]


def retrieve(query: str) -> tuple[str, str]:
    """Top-1 doc by word overlap. Returns (doc_id, text)."""
    q = set(query.lower().split())
    best = max(DOCS, key=lambda d: len(q & set(d[1].lower().split())))
    return best


def answer(mon: Monitor, question: str) -> dict:
    with mon.trace("faq_request", question=question) as tid:
        with mon.step("retrieve", kind="retrieval") as s:
            doc_id, context = retrieve(question)
            s.set("gen_ai.retrieval.doc_id", doc_id)
        prompt = (
            "Answer the question using only the context.\n"
            f"Context: {context}\nQuestion: {question}\nAnswer:"
        )
        out = mon.chat(prompt)
    return {"trace_id": tid, "doc_id": doc_id, "text": out["text"]}


def main() -> None:
    mon = Monitor("monitor.db")
    questions = [" ".join(sys.argv[1:])] if len(sys.argv) > 1 else QUESTIONS
    for q in questions:
        r = answer(mon, q)
        print(f"Q: {q}")
        print(f"   [{r['doc_id']}] {r['text']}")
        print(f"   trace: {r['trace_id']}\n")


if __name__ == "__main__":
    main()
