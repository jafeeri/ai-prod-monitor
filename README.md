# AI Prod Monitor

Watch a live LLM app the way you'd watch any other production service: every request traced, every token priced, a sample of the traffic scored for quality, and an alert when quality slips or the bill jumps.

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)
![Dependencies](https://img.shields.io/badge/core-zero--dependency-lightgrey.svg)
![Backends](https://img.shields.io/badge/backends-mock%20%7C%20ollama%20%7C%20anthropic-orange.svg)

## Why I built this

Offline evals tell you whether a change broke a case you already thought of. They run before you ship, against a fixed set, and they are necessary. They are also not enough, because the moment real users show up three things happen that an offline eval never sees.

First, people send inputs you never wrote a test for, and quality quietly rots on cases you have no gold answer for. Second, the bill tracks tokens, not requests, so one user pasting a long document can cost more than a thousand short chats while your request counter looks calm. Third, when a multi step chain returns garbage, the final answer tells you nothing about which step broke, and because the model is non deterministic you often cannot reproduce it.

This tool lives on the other side of that line. It sits next to a running app, records what actually happened on each request, prices the tokens, judges a slice of the output, and pages you when a trend goes bad. You then click from the alert straight into the exact trace that caused it.

## What it does

- Traces each request as a tree of spans (retrieval, LLM call, tool call). Every LLM span carries the model, input and output token counts, and latency. The prompt and response are stored as span events, so they stay searchable but never bloat the rows you aggregate over.
- Prices tokens per model, input and output separately, and rolls the cost up per request, per model, and over time.
- Scores a sample of live traffic with an LLM as judge across relevance, faithfulness, and safety. Judging runs off the request path, so it never adds cost or latency to a real user's call.
- Compares a recent window against a rolling baseline for quality, cost, and latency, and flags a move once it crosses a threshold.
- Raises an alert when a threshold breaks, records it with the offending trace id, and lets you open that trace to find the cause.
- Shows all of it on a small live dashboard.

It attaches to any LLM app through a two line SDK, and it ships with a self contained demo app so you can see it working before you touch your own code.

## Install

```bash
git clone https://github.com/jafeeri/ai-prod-monitor.git
cd ai-prod-monitor
pip install -r requirements.txt   # only the dashboard needs a dependency
```

Python 3.10 or newer. The tracer, SDK, cost, eval, drift, alerting, and CLI use only the standard library. Streamlit is the one runtime dependency, and only the dashboard uses it.

## Quickstart

Everything runs offline on a deterministic mock backend, so you need no API key to try it.

```bash
python demo_app.py            # generate some traffic
python -m monitor watch       # sample and judge new traffic, check drift, raise alerts
python -m monitor report      # cost, quality, and drift status
python -m monitor show 10153a74267b48a4   # drill into one trace (use a real id from the output)
```

Then open the dashboard:

```bash
streamlit run dashboard.py -- --db monitor.db --watch 5
```

![The dashboard](docs/dashboard.png)

## Instrument your own app

You route your LLM calls through the monitor. That is the whole integration.

```python
from monitor import Monitor

mon = Monitor("monitor.db")

def answer(question):
    with mon.trace("faq_request", question=question):
        with mon.step("retrieve", kind="retrieval") as s:
            docs = my_retriever(question)
            s.set("gen_ai.retrieval.k", len(docs))
        result = mon.chat(build_prompt(docs, question))   # traced LLM call
    return result["text"]
```

`mon.chat()` makes the call, records the model, tokens, latency, and cost, and stores the prompt and response as events. `mon.step()` wraps any sub step (retrieval, a tool call) as its own span. If you already have your own LLM function, wrap it as a span with the decorator:

```python
@mon.wrap(kind="tool")
def call_weather_api(city):
    ...
```

A top level `mon.chat()` or `mon.step()` works on its own too. If there is no trace open, it opens one for that single call, so you can instrument gradually without wrapping everything at once.

## The command line

```
python -m monitor watch [db]       one tick: sample and judge new traffic, check drift, raise alerts
python -m monitor show <trace_id>  the span tree for one request, with tokens, latency, cost, and the prompt and response
python -m monitor report [db]      cost rollup, quality summary, and current drift status
```

`watch` is the unit you run on a loop for continuous alerting. In Claude Code that is one line:

```
/loop 5m python -m monitor watch
```

Or use cron, a systemd timer, or any scheduler you already have.

## How the pieces work

### Cost

Cost is derived, never guessed. For each LLM span it is `input_tokens * input_rate + output_tokens * output_rate`, priced per model, and the price used is stored on the span so your history stays correct even after you edit the table. The rates live in `monitor/cost.py`. They are sensible defaults, not gospel. Check them against each provider's current pricing page before you trust the dollar figures. Local and mock backends are free, so they price at zero.

### Online evaluation

You cannot judge every request without doubling your cost, so the monitor samples. Sampling is deterministic per span id, which means a re run scores the same spans and never double counts. The judge is a rubric prompt: it scores relevance, faithfulness, and safety on a one to five scale, explains itself before giving a number, and is told that a longer answer is not a better one.

A judge is a measuring instrument, and an instrument you have not checked is not one you should trust. `calibration_agreement()` compares the judge against a small set of human labels so you can see how well they agree before you rely on the scores. On a bad or unparseable judge response the score comes back null. It is never faked.

### Drift and alerts

A single bad request is noise. A sustained move is a signal. Drift compares the most recent window of each metric against the window just before it, and flags quality drops, latency rises, and cost spikes once they pass a threshold. When one breaks, an alert is written with the trace that best explains it (the worst scoring, slowest, or priciest recent request), and a per metric cooldown keeps a long running problem from firing on every tick. From the alert you open that trace and see exactly what happened.

## Configuration

Set these in the environment or a `.env` file (see `.env.example`).

```
MONITOR_LLM=mock|ollama|anthropic     # default mock
ANTHROPIC_API_KEY=...                  # only for the anthropic backend
MONITOR_MAX_EVENT_CHARS=16384          # truncate huge prompt/response payloads; 0 disables
```

To run against a real model:

```bash
# local, free
ollama serve && ollama pull llama3.2
export MONITOR_LLM=ollama
python demo_app.py && python -m monitor report

# Claude
export MONITOR_LLM=anthropic
export ANTHROPIC_API_KEY=sk-ant-...
python demo_app.py && python -m monitor report
```

Keys are read from the environment only. Nothing is printed, and `.env` is git ignored.

## How it is built

The store is a single SQLite file with WAL enabled, so the dashboard can read while your app writes without either one blocking the other. Each thread gets its own connection, which is what lets concurrent requests trace correctly instead of stepping on each other. The current trace and span are tracked with context variables, so nesting is right across threads and async code.

Attribute names follow the OpenTelemetry GenAI semantic conventions (`gen_ai.request.model`, `gen_ai.usage.input_tokens`, and so on). The storage is custom and small, but because the vocabulary matches the standard, an OTLP exporter later is an adapter, not a rewrite.

```
your app --SDK--> tracer --> SQLite (traces, spans, events)
                                |
        +-----------------------+------------------------+
        v                       v                        v
   cost rollups          sampler -> judge -> scores      |
        |                       |                        |
        v                       v                        |
   report/dashboard      drift vs baseline               |
                                v                        |
                         threshold break -> alert (+trace id)
                                v
                         open the trace, find the cause
```

## Testing

```bash
python selfcheck.py              # fast regression: every module's self check
python tests/adversarial_test.py # failure paths: concurrency, fresh db, provider errors, fuzzing
```

The adversarial suite hammers the parts that usually break in production: twenty threads writing at once, reads against an empty database, a provider that throws mid call, and fuzzed prompts (empty, a megabyte long, unicode, control characters, null bytes, JSON). It should report zero failures. There is also a property based test in `tests/` if you install `hypothesis`.

## Limitations

This is a single node tool. The SQLite store is the right call for one service and a local dashboard, and it is not built for multi node, high throughput ingest. If you get there, put an OTLP collector in front and export to it (the attribute names already line up for that). The pricing table is a starting point you should verify, and the LLM judge, like any judge, needs calibrating against human labels before you lean on its numbers.

## License

MIT. See [LICENSE](LICENSE). Copyright (c) 2026 Ali Mehdi Jafeeri.
