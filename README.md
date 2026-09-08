# AI Prod Monitor

A dashboard and alarm system for apps that use AI. If you have built something on top of a language model (a chatbot, a support assistant, a document summarizer, anything that calls GPT, Claude, Gemini, or a local model), this watches it while it runs. It records what every request did, adds up what it costs, checks a sample of the answers for quality, and warns you when something starts going wrong.

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)
![Core](https://img.shields.io/badge/core-zero--dependency-lightgrey.svg)
![Backends](https://img.shields.io/badge/works%20with-openai%20%7C%20claude%20%7C%20gemini%20%7C%20ollama-orange.svg)

![The dashboard](docs/dashboard.png)

## Table of contents

- [Read this first (plain English)](#read-this-first-plain-english)
- [Who this is for](#who-this-is-for)
- [The words you will see, explained](#the-words-you-will-see-explained)
- [Install](#install)
- [Five minute quickstart](#five-minute-quickstart-no-api-key)
- [Pick your model](#pick-your-model)
- [Add it to your own app](#add-it-to-your-own-app)
- [The command line](#the-command-line)
- [The dashboard](#the-dashboard)
- [How each piece works](#how-each-piece-works)
- [Configuration](#configuration)
- [Under the hood](#under-the-hood-for-the-curious)
- [Testing](#testing)
- [FAQ and troubleshooting](#faq-and-troubleshooting)
- [Limitations](#limitations)
- [License](#license)

## Read this first (plain English)

When you build a normal website, you can watch it with tools that tell you how many people visited, how fast pages loaded, and whether anything crashed. Apps built on AI need the same thing, plus two problems that normal apps do not have.

The first problem is that AI answers are not simply right or wrong. A summary can be technically correct and still miss the point. So you cannot just count errors. You have to actually judge the quality of the answers, and you cannot pay a human to read all of them.

The second problem is money. Every time your app talks to an AI model, you pay by the amount of text going in and out, measured in units called tokens. This means one user who pastes in a huge document can cost more than a thousand users asking short questions. If you only count requests, the bill can surprise you.

This project handles both. It sits quietly next to your app, keeps a detailed record of every AI call (including the cost), automatically grades a sample of the answers, and sends you an alert the moment quality drops or spending spikes. When it alerts you, it hands you the exact request that caused the problem so you can see what happened.

You do not need an API key or any paid account to try it. It ships with a fake AI backend so you can run the whole thing on your laptop in a couple of minutes.

## Who this is for

- You built an app that calls an AI model and you want to know if it is doing a good job in the real world.
- You are worried about the AI bill and want to see where the money goes.
- Something in a multi step AI pipeline occasionally returns nonsense and you cannot tell which step broke.
- You are learning how production AI systems are monitored and want a small, readable codebase to study.

You do not need to be an AI expert. If you can run a Python script, you can use this.

## The words you will see, explained

You will meet a few terms in this tool and in the AI world generally. Here they are in one place.

- **Token.** A chunk of text, very roughly three quarters of a word. Models read and write in tokens, and you pay per token. "500 tokens in, 200 tokens out" means the prompt was about 375 words and the answer about 150.
- **Prompt and response.** The prompt is what you send the model. The response is what it sends back.
- **Trace.** The full record of one request through your app, start to finish. Think of it as a flight recorder for a single question your user asked.
- **Span.** One step inside a trace. If answering a question means looking something up and then calling the model, that is two spans (a retrieval span and an LLM span) inside one trace.
- **LLM.** Large language model. The AI itself: GPT, Claude, Gemini, Llama, and so on.
- **Latency.** How long something took, in milliseconds.
- **LLM as judge.** Using an AI model to grade the answers of another AI model. It turns out that checking an answer is much easier than writing one, so this works reasonably well when done carefully.
- **Drift.** A slow slide in quality, cost, or speed over time, as opposed to one bad request. Drift is what you actually want to catch, because it is the early warning.
- **Backend or provider.** Where the AI model runs: OpenAI, Anthropic, Google Gemini, or a local model on your own machine through Ollama.

## Install

You need Python 3.10 or newer. If you do not have Python, get it from [python.org](https://www.python.org/downloads/), then open a terminal.

```bash
git clone https://github.com/jafeeri/ai-prod-monitor.git
cd ai-prod-monitor
pip install -r requirements.txt
```

That last command installs one thing, Streamlit, which powers the dashboard. Everything else uses only what already comes with Python, so there is nothing heavy to install and nothing that can rot over time.

## Five minute quickstart (no API key)

By default the tool uses a fake model called `mock`. It gives instant, made up answers so you can see the whole system work without signing up for anything or spending a cent.

```bash
python demo_app.py            # a tiny built-in app makes a few AI calls
python -m monitor watch       # grade a sample of them and check for problems
python -m monitor report      # print cost, quality, and drift
```

The demo is a small question and answer app included in the repo. It stands in for your app so you have something to monitor on day one.

To see a trace in full, copy a trace id printed by the demo and run:

```bash
python -m monitor show 10153a74267b48a4
```

That prints the step by step record of one request: what it retrieved, what it sent the model, the tokens, the latency, the cost, and the full prompt and answer.

Then open the live dashboard in your browser:

```bash
streamlit run dashboard.py -- --db monitor.db --watch 5
```

The `--watch 5` part makes it refresh every five seconds so you can leave it open while your app runs.

## Pick your model

The whole point is that you are not locked into any one AI provider. You choose with a single setting, `MONITOR_LLM`. Bring your own model.

| You want to use | Set `MONITOR_LLM` to | Also set |
| --- | --- | --- |
| Nothing, just try it offline | `mock` (this is the default) | nothing |
| A local model on your own machine | `ollama` | run Ollama, `ollama pull llama3.2` |
| OpenAI (GPT) | `openai` | `OPENAI_API_KEY` |
| Anthropic (Claude) | `anthropic` | `ANTHROPIC_API_KEY` |
| Google (Gemini) | `gemini` | `GEMINI_API_KEY` |

For example, to run the demo against OpenAI on macOS or Linux:

```bash
export MONITOR_LLM=openai
export OPENAI_API_KEY=sk-...
python demo_app.py && python -m monitor report
```

On Windows PowerShell the same thing is:

```powershell
$env:MONITOR_LLM="openai"
$env:OPENAI_API_KEY="sk-..."
python demo_app.py; python -m monitor report
```

Prefer to keep nothing paid running? Ollama lets you run a capable model locally for free:

```bash
ollama serve
ollama pull llama3.2
export MONITOR_LLM=ollama
python demo_app.py && python -m monitor report
```

A note on cost figures: the tool knows the published prices for common OpenAI, Anthropic, and Gemini models and works out the dollar cost for you. Providers change their prices, so treat the numbers as close, not exact, and check `monitor/cost.py` if you need them precise. Local and mock models are free, so they show up as zero.

## Add it to your own app

This is the part that matters once you are past the demo. You point your existing AI calls at the monitor. There are three ways in, and you can mix them.

Here is a complete example. Say your app answers questions by looking something up and then asking a model:

```python
from monitor import Monitor

mon = Monitor("monitor.db")   # this file is where everything is recorded

def answer(question):
    with mon.trace("faq_request", question=question):
        # wrap your retrieval step so it shows up in the trace
        with mon.step("retrieve", kind="retrieval") as s:
            docs = my_retriever(question)
            s.set("gen_ai.retrieval.k", len(docs))

        # make the AI call through the monitor instead of directly
        result = mon.chat(build_prompt(docs, question))

    return result["text"]
```

What each part does:

- `mon.trace(...)` starts the recording for one request. Give it a name and any details you want to keep.
- `mon.step(...)` records a sub step, like a database lookup or a tool call, as its own span.
- `mon.chat(...)` is the important one. It makes the actual model call, and along the way it records the model used, the tokens in and out, how long it took, the cost, and the full prompt and answer. It returns the normal result, so `result["text"]` is your answer.

If you already have your own function that calls a model and you do not want to change how it works, just decorate it and it becomes a tracked step:

```python
@mon.wrap(kind="tool")
def call_weather_api(city):
    ...
```

You do not have to wrap everything at once. A lone `mon.chat(...)` works on its own, even with no `mon.trace(...)` around it. In that case it quietly starts a trace just for that one call. So you can add monitoring one call at a time and grow into it.

## The command line

Once your app is writing to `monitor.db`, these three commands are how you look at it.

```
python -m monitor watch [db]       Do one round: grade a sample of new traffic, check for drift,
                                   and raise any alerts. This is the one you run on a schedule.

python -m monitor show <trace_id>  Show one request in full: every step, with tokens, latency,
                                   cost, and the prompt and response.

python -m monitor report [db]      Print the cost breakdown, the quality summary, and the current
                                   drift status.
```

The `[db]` is optional and defaults to `monitor.db`. For `show`, paste a real trace id from the demo or a report. Do not type the angle brackets. On PowerShell in particular, `<` and `>` are special characters, so `python -m monitor show <id>` will error. Use `python -m monitor show 10153a74267b48a4`.

To keep watch continuously, run `watch` on a loop. Any scheduler works: cron, a systemd timer, Windows Task Scheduler, or if you use Claude Code, one line does it:

```
/loop 5m python -m monitor watch
```

## The dashboard

```bash
streamlit run dashboard.py -- --db monitor.db --watch 5
```

The dashboard reads the same database your app writes to, so it never gets in your app's way. At the top you get the headline numbers: total requests, total AI calls, total tokens, total cost, and the current average quality score. Below that are the drift and alert cards, which turn red when a threshold is crossed. Then come the breakdowns: cost by model, a latency chart, a quality over time chart, cost per request, and a list of recent requests you can investigate.

## How each piece works

### Cost

For every AI call, the cost is worked out as the input tokens times the input price plus the output tokens times the output price, using the price for that specific model. Input and output are priced separately because providers charge different rates for each, often several times more for output. The price used is saved with the record, so your history stays accurate even if you update the price table later.

### Quality (online evaluation)

You cannot afford to grade every single answer, because grading means another AI call, which costs money and time. So the tool grades a sample instead. It picks which requests to grade in a way that is stable, meaning if you run the grading again it grades the same ones and never double counts.

The grading itself uses an AI model as a judge, with a clear rubric. It scores each answer from one to five on three things: relevance (did it actually answer the question), faithfulness (did it stick to the facts it was given), and safety. It is told to explain its reasoning before giving a score and that a longer answer is not automatically a better one.

One honest warning, which applies to every tool that does this. An AI judge has its own biases and is not a perfect stand in for a human. Before you trust the scores, check them against a handful of answers you grade by hand. The tool includes a function, `calibration_agreement()`, for exactly this. And if the judge ever returns something the tool cannot read, the score comes back empty rather than being made up.

### Drift and alerts

One bad answer is noise. A downward trend is a signal. The tool keeps comparing the most recent stretch of activity against the stretch just before it. When quality drops, or latency climbs, or cost jumps past a threshold you set, it raises an alert. The alert is saved with the id of the request that best explains it, so you can jump straight from the alert to the actual trace and see the cause. A cooldown stops a long running problem from alerting on every single check.

## Configuration

Set these in your environment, or copy `.env.example` to `.env` and edit it. The `.env` file is ignored by git, so your keys never get committed.

```
MONITOR_LLM=mock|ollama|openai|anthropic|gemini   # which model to use, default mock
OPENAI_API_KEY=...                                 # only for openai
ANTHROPIC_API_KEY=...                              # only for anthropic
GEMINI_API_KEY=...                                 # only for gemini
MONITOR_MAX_EVENT_CHARS=16384                      # cap on stored prompt/response size, 0 = no cap
```

Keys are read from the environment only. They are never printed and never written to the database.

## Under the hood (for the curious)

If you want to know how it is built, or you are reading the code to learn, here is the shape of it.

The store is a single SQLite file. SQLite is a full database that lives in one file with no server to run, which is exactly right for a tool that watches one service. It runs in a mode called WAL (write ahead logging) so the dashboard can read at the same time your app writes, without either one blocking the other. Each thread gets its own database connection, which is what lets many requests be traced at the same time without tripping over each other. The current trace and span are tracked with Python context variables, so nesting stays correct even in threaded or async code.

The field names follow the OpenTelemetry standard for AI (names like `gen_ai.request.model` and `gen_ai.usage.input_tokens`). OpenTelemetry is the common language monitoring tools speak. The storage here is small and custom, but because it uses the standard names, exporting to a full OpenTelemetry backend later is an adapter, not a rewrite.

The flow, end to end:

```
your app --SDK--> tracer --> SQLite (traces, spans, events)
                                |
        +-----------------------+------------------------+
        v                       v                        v
   cost rollups          sample -> AI judge -> scores    |
        |                       |                        |
        v                       v                        |
   report / dashboard    drift vs baseline               |
                                v                        |
                         threshold crossed -> alert (with trace id)
                                v
                         open that trace, find the cause
```

The code is deliberately small. Each file has a self check at the bottom you can run, and the whole thing is meant to be read in an afternoon.

## Testing

```bash
python selfcheck.py              # quick check that every module works
python tests/adversarial_test.py # the hard tests: does it survive real trouble
```

The second one is the interesting suite. It throws the kind of trouble that actually happens in production at the tool: twenty requests writing at the same instant, reads against an empty database, an AI provider that fails in the middle of a call, and deliberately nasty inputs (empty, a megabyte long, emoji, control characters, null bytes, raw JSON). It should report zero failures. There is also a property based test in `tests/` if you install `hypothesis`, which invents hundreds of random inputs to try to break the cost math.

## FAQ and troubleshooting

**Do I need to pay for anything to use this?**
No. It runs offline on a fake model by default. You only pay if you point it at a paid provider like OpenAI, Anthropic, or Gemini, and even then you are paying that provider directly, not this tool.

**Will this itself cost me a lot in AI calls?**
The only extra AI calls it makes are for grading, and it grades a sample, not everything. You control the sample rate. If you use a local model through Ollama, grading is free too.

**Which model should I use?**
For trying it out, the default `mock` is fine. For real use, any of the four providers works. Local models through Ollama are free and private but slower. The hosted providers are faster and stronger but cost money. Start with whatever you already have a key for.

**I heard Ollama can be flaky. Does that matter?**
Not to this tool. The provider is just one setting. If a local model gives you trouble, switch `MONITOR_LLM` to a hosted provider and nothing else changes. The monitor treats every backend the same way.

**A model call failed. Did it crash my app?**
The monitor does not hide errors from your app, so your app sees the failure as it normally would. But it does record the failure on the trace, marked as an error, so you can see it happened and why.

**Where is my data?**
In the SQLite file you named, on your own machine. Nothing is sent anywhere except the AI provider you chose, and only the prompts you send it. Your API keys stay in your environment.

**The `show` command errors on Windows.**
You probably typed the angle brackets. Use a real id with no brackets: `python -m monitor show 10153a74267b48a4`.

## Limitations

This is a single machine tool by design. One SQLite file and a local dashboard are the right size for watching one service, and this is not built for spreading across many machines at very high volume. If you get to that scale, put an OpenTelemetry collector in front of your app and export to it. The field names already line up for that. Two more honest notes: the price table is a helpful default you should verify against current provider prices, and the AI judge needs a quick sanity check against your own judgment before you lean on its scores.

## License

MIT. See [LICENSE](LICENSE). Copyright (c) 2026 Ali Mehdi Jafeeri.
