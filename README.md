# llm-otel-reference

A reference for observing a Claude tool-using agent with OpenTelemetry, on a self-hosted
Grafana stack (Loki, Tempo, Prometheus, Grafana).

```
agent ──OTLP──▶ OpenTelemetry Collector
                  traces/in:  strip prompt text ─▶ span_metrics (100% of spans) ─▶ Prometheus
                                               └─▶ forward
                  traces/out: tail sampling (all errors, slow > 20s, 10% of the rest) ─▶ Tempo
                  logs:       strip prompt text ─▶ Loki (trace_id links each line to its trace)
                  metrics:    app metrics + span_metrics ─▶ Prometheus (native OTLP)
```

Every request produces one trace:

```
invoke_agent catalog-agent        tokens and dollar cost rolled up for the whole request
├── chat claude-sonnet-5          one span per model call: tokens, cost, finish reason
├── execute_tool get_product      one span per tool call: tool name, call id, errors
└── chat claude-sonnet-5
```

Spans follow the OpenTelemetry GenAI semantic conventions (still experimental). Prompt and
response text never go on spans; only counts, ids, and outcomes do.

## Run it

```bash
docker compose up -d                      # Collector, Tempo, Loki, Prometheus, Grafana (:3001, admin/admin)
cp .env.example .env                      # add your ANTHROPIC_API_KEY
set -a; source .env; set +a
uv run uvicorn --factory agent.app:build_app --port 8000
curl -s localhost:8000/ask -H 'content-type: application/json' \
  -d '{"question": "What does a 2-person tent plus a canister stove cost and weigh?"}'
```

Then open Grafana, go to Explore, pick Tempo, and search for service `catalog-agent`.
Tail sampling keeps 10% of successful traces, so set `TRACE_SAMPLE_PERCENT=100` before
`docker compose up` if you want to see every one while exploring.

## Prove the Collector policies

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 uv run python scripts/verify_stack.py
```

Sends 60 traces through the real agent code with a fake Claude client ($0), then checks:
errors are always kept, successes are sampled, captured prompt text never reaches Tempo,
each request's log line links to its trace, and span metrics count all 60 requests.

## SLOs and alerts

| SLO | Target | Budget (28 days) |
|---|---|---|
| Availability: requests end with `outcome="ok"` | 99% | 1% |
| Latency: requests finish in 20.48s or less | 95% | 5% |

`prometheus/rules/slo.yml` records error and slow ratios over 5m, 30m, 1h, and 6h, and
alerts with the multi-window, multi-burn-rate pattern from the Google SRE Workbook:
page at 14.4x burn (1h and 5m), ticket at 6x (6h and 30m). A separate guardrail opens a
ticket when model spend passes $1 an hour. Synthetic traffic from `verify_stack.py` is
excluded from every SLO query.

The rules have unit tests:

```bash
docker run --rm -v "$PWD/prometheus/rules:/rules:ro" --entrypoint promtool \
  prom/prometheus:v3.14.0 test rules /rules/slo_test.yml
```

## Dashboard

Grafana opens straight to the dashboard at http://localhost:3001 (read-only, no login).
It is generated from `scripts/build_dashboard.py`; edit that file, never the JSON:

```bash
uv run python scripts/build_dashboard.py     # regenerate grafana/dashboards/catalog-agent.json
uv run python scripts/verify_dashboard.py    # run every panel query against the live stack
```

Rows: SLOs and firing alerts, traffic and latency (with exemplars that link to traces),
cost and tokens, and agent behavior from span metrics (model calls per request, tool calls).

## Load test

`load/k6-agent.js` runs 1, 5, then 20 concurrent users, 15 requests each. The request
count is capped by construction (390), so the cost is too.

```bash
k6 run load/k6-agent.js                              # full run, ~9 minutes, ~$1 on Haiku
ITERS=1 STAGE_GAP_S=90 k6 run load/k6-agent.js       # smoke, 26 requests
```

Results, 2026-09-28, `claude-haiku-4-5`, one local agent process:

| Users | Throughput | Median | p95 | Failed | $/request | Model calls/request | Spend rate |
|---|---|---|---|---|---|---|---|
| 1 | ~0.26 req/s | 4.1s | 7.1s | 0% | $0.0025 | 2.13 | ~$2/hour |
| 5 | ~1.5 req/s | 2.9s | 6.2s | 0% | $0.0025 | 2.15 | ~$13/hour |
| 20 | ~7.6 req/s | 2.2s | 4.9s | 0% | $0.0025 | 2.13 | ~$69/hour |

Throughput is users divided by average latency (k6 users loop with no think time).
Spend rate is throughput times $0.0025 times 3600.

Total: 390 requests, $0.97 by k6's count and $0.98 by the `llm_cost_usd_total` metric
(the 1% gap is `increase()` extrapolating to the window edges). No upstream 429s.

What it showed:

- **20 users did not find the limit.** At ~7.6 requests per second latency fell and
  nothing failed. The knee is higher. The drop in latency under load is unexplained; I
  have not tested a cause.
- **Cost scales linearly and is the real constraint.** At the 20-user rate this agent
  spends about $69 an hour at list price, while still fast. Cost per request did not move
  with load. `AgentSpendHigh` went from pending to firing after the run (Prometheus
  `/api/v1/alerts`, and the Firing alerts panel in the screenshot below).
- **Search is the expensive tool.** Over the run, span metrics counted 431
  `search_catalog` calls and 113 `calculate_total` calls, about 3.8 to 1. The search tool
  matches substrings only ("2-person" misses "Two-person"), so the model retries with
  new wording, and each retry is a paid model call.
- **The dashboard's 5-minute rate windows flatten short bursts.** The 20-user stage lasted
  about 40 seconds, so the panels show ~1 request per second and ~$10 an hour, not the
  real peak. Read peaks from k6; read sustained rates from the dashboard.

![Dashboard during the load test](docs/dashboard-load-test.png)

## Prompt capture

Off by default. `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true` puts prompt and
answer text on chat spans as `gen_ai.input.messages` and `gen_ai.output.messages`. The
Collector deletes both before storage. The policy lives in one place, not in every service.

## Test it

```bash
uv run pytest --cov=agent
```

Tests use a scripted fake Claude client and an in-memory span exporter. No API key needed.

## Cost

`agent/pricing.yaml` holds dated list prices. Every chat span carries `llm.cost.usd`; the
root span carries the request total. An unknown model raises instead of reporting $0.

## Metrics

| Prometheus series | What it answers |
|---|---|
| `gen_ai_client_token_usage` (by `gen_ai_token_type`) | Input vs output tokens per model call |
| `gen_ai_client_operation_duration_seconds` | Latency of each model call, with `error_type` on failure |
| `llm_agent_request_duration_seconds` (by `outcome`) | End-to-end latency; the SLO input |
| `llm_agent_request_cost` | Dollar cost per request at list price |
| `llm_cost_usd_total` (by model) | Running spend, including failed requests that were still billed |

Example: average cost per request is
`sum(llm_agent_request_cost_sum) / sum(llm_agent_request_cost_count)`.

## Roadmap

1. Agent, GenAI spans, all-in-one stack (done)
2. Token, cost, and latency metrics (done)
3. Dedicated Collector: span metrics, prompt redaction, tail sampling, logs linked to traces (done)
4. Dashboards and SLOs as code, burn-rate alerts (done)
5. k6 load test at 1, 5, and 20 users: latency, 429s, and dollars per request (done)
