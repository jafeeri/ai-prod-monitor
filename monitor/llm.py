"""Provider-agnostic LLM client. One function, three backends, zero deps.

    MONITOR_LLM = mock (default) | ollama | anthropic

Returns a uniform dict so the tracer always gets tokens + model, no matter the
backend:  {"text", "model", "tokens_in", "tokens_out", "system"}.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.request


def _approx_tokens(text: str) -> int:
    # ponytail: chars/4 heuristic — good enough for the mock backend. Real
    # backends return exact usage; only the mock estimates.
    return max(1, round(len(text) / 4))


def _mock(prompt: str, model: str | None) -> dict:
    # Deterministic so offline self-checks stay green. Answer echoes a short,
    # stable digest of the prompt so different inputs give different outputs.
    tag = hashlib.sha1(prompt.encode()).hexdigest()[:6]
    text = f"[mock:{tag}] " + " ".join(prompt.split()[:12])
    return {
        "text": text,
        "model": model or "mock-1",
        "tokens_in": _approx_tokens(prompt),
        "tokens_out": _approx_tokens(text),
        "system": "mock",
    }


def _ollama(prompt: str, model: str | None) -> dict:
    body = json.dumps(
        {"model": model or "llama3.2", "prompt": prompt, "stream": False}
    ).encode()
    req = urllib.request.Request(
        "http://localhost:11434/api/generate", data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        r = json.loads(resp.read())
    return {
        "text": r.get("response", ""),
        "model": r.get("model", model or "llama3.2"),
        "tokens_in": int(r.get("prompt_eval_count", _approx_tokens(prompt))),
        "tokens_out": int(r.get("eval_count", 0)),
        "system": "ollama",
    }


def _anthropic(prompt: str, model: str | None) -> dict:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("MONITOR_LLM=anthropic but ANTHROPIC_API_KEY is not set")
    model = model or "claude-sonnet-5"
    body = json.dumps({
        "model": model,
        "max_tokens": 512,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={
            "content-type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        r = json.loads(resp.read())
    text = "".join(b.get("text", "") for b in r.get("content", []))
    usage = r.get("usage", {})
    return {
        "text": text,
        "model": r.get("model", model),
        "tokens_in": int(usage.get("input_tokens", 0)),
        "tokens_out": int(usage.get("output_tokens", 0)),
        "system": "anthropic",
    }


_BACKENDS = {"mock": _mock, "ollama": _ollama, "anthropic": _anthropic}


def complete(prompt: str, model: str | None = None) -> dict:
    """Call the configured LLM backend and return uniform usage."""
    backend = os.environ.get("MONITOR_LLM", "mock").lower()
    fn = _BACKENDS.get(backend)
    if fn is None:
        raise ValueError(f"unknown MONITOR_LLM={backend!r}; use {list(_BACKENDS)}")
    return fn(prompt, model)


if __name__ == "__main__":
    r = complete("What is observability in one sentence?")
    assert r["text"] and r["tokens_in"] > 0 and r["tokens_out"] > 0, r
    print("llm self-check OK:", os.environ.get("MONITOR_LLM", "mock"),
          "->", r["model"], f'{r["tokens_in"]}in/{r["tokens_out"]}out')
