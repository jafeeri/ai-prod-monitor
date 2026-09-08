"""Cost accounting: price tokens, roll up spend.

Cost is derived, never guessed: cost = tokens_in * in_rate + tokens_out * out_rate,
priced PER MODEL and separately for input vs output (they differ, often 3-5x).

PRICING is USD per 1,000,000 tokens. These are editable defaults — verify against
each provider's current pricing page before trusting the dollar figures; providers
change prices. Local/mock backends are free.
"""
from __future__ import annotations

from . import store

# model -> (input $/1M tokens, output $/1M tokens). Verify before relying on $.
# These are editable defaults; providers change prices. Add your own models here.
PRICING: dict[str, tuple[float, float]] = {
    # Anthropic
    "claude-opus-5":   (15.00, 75.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    # OpenAI
    "gpt-4o":          (2.50, 10.00),
    "gpt-4o-mini":     (0.15, 0.60),
    # Google Gemini
    "gemini-1.5-pro":   (1.25, 5.00),
    "gemini-1.5-flash": (0.075, 0.30),
    "gemini-2.0-flash": (0.10, 0.40),
    # Local / free
    "mock-1":          (0.0, 0.0),
    "llama3.2":        (0.0, 0.0),
}

# Unknown models cost 0 by default (so nothing silently inflates the bill on a
# typo) — set a non-zero fallback here if you'd rather over- than under-count.
DEFAULT_RATE: tuple[float, float] = (0.0, 0.0)


def cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    rin, rout = PRICING.get(model, DEFAULT_RATE)
    # clamp: negative token counts (bad upstream data) must never yield negative cost
    ti, to = max(0, tokens_in or 0), max(0, tokens_out or 0)
    return ti * rin / 1e6 + to * rout / 1e6


# ---- rollups (plain SQL over the columns tracer projects) -----------------
def _rows(db_path: str, sql: str, args=()) -> list[dict]:
    db = store.connect(db_path)  # ensures schema → safe on a fresh db
    try:
        return [dict(r) for r in db.execute(sql, args).fetchall()]
    finally:
        db.close()


def by_model(db_path: str) -> list[dict]:
    return _rows(db_path, """
        SELECT COALESCE(model,'(unknown)') AS model,
               COUNT(*)                AS calls,
               SUM(tokens_in)          AS tokens_in,
               SUM(tokens_out)         AS tokens_out,
               ROUND(SUM(cost_usd), 6) AS cost_usd,
               ROUND(AVG(latency_ms), 1) AS avg_latency_ms
        FROM spans WHERE kind='llm'
        GROUP BY model ORDER BY cost_usd DESC""")


def by_trace(db_path: str) -> list[dict]:
    return _rows(db_path, """
        SELECT s.trace_id,
               t.name                  AS trace,
               COUNT(*)                AS llm_calls,
               SUM(s.tokens_in)        AS tokens_in,
               SUM(s.tokens_out)       AS tokens_out,
               ROUND(SUM(s.cost_usd), 6) AS cost_usd
        FROM spans s JOIN traces t ON t.trace_id=s.trace_id
        WHERE s.kind='llm'
        GROUP BY s.trace_id ORDER BY cost_usd DESC""")


def totals(db_path: str) -> dict:
    r = _rows(db_path, """
        SELECT COUNT(DISTINCT trace_id)  AS traces,
               COUNT(*)                  AS llm_calls,
               SUM(tokens_in)            AS tokens_in,
               SUM(tokens_out)           AS tokens_out,
               ROUND(SUM(cost_usd), 6)   AS cost_usd,
               ROUND(AVG(latency_ms), 1) AS avg_latency_ms,
               ROUND(MAX(latency_ms), 1) AS max_latency_ms
        FROM spans WHERE kind='llm'""")
    return r[0] if r else {}


def _selfcheck() -> None:
    import os
    import tempfile
    from .sdk import Monitor

    os.environ["MONITOR_LLM"] = "mock"
    path = os.path.join(tempfile.mkdtemp(), "cost.db")

    # cost math: separate in/out rates
    assert cost_usd("claude-sonnet-5", 1_000_000, 0) == 3.00
    assert cost_usd("claude-sonnet-5", 0, 1_000_000) == 15.00
    assert cost_usd("claude-sonnet-5", 1_000_000, 1_000_000) == 18.00
    assert cost_usd("unknown-model", 999, 999) == 0.0  # unknown → 0, no silent inflation

    # end-to-end: chat should attach cost, rollups should sum it
    mon = Monitor(path)
    with mon.trace("t"):
        mon.chat("hello world how are you", model="claude-sonnet-5")
    tot = totals(path)
    assert tot["llm_calls"] == 1 and tot["cost_usd"] > 0, tot
    bm = by_model(path)
    assert bm[0]["model"] == "claude-sonnet-5" and bm[0]["cost_usd"] == tot["cost_usd"], bm
    print(f"cost self-check OK: 1 call, ${tot['cost_usd']:.6f}, "
          f"{tot['tokens_in']}in/{tot['tokens_out']}out")


if __name__ == "__main__":
    _selfcheck()
