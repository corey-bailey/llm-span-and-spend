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
4. Dashboards and SLOs as code, burn-rate alerts
5. k6 load test at 1, 5, and 20 users: latency, 429s, and dollars per request
