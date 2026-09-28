# llm-otel-reference

A reference for observing a Claude tool-using agent with OpenTelemetry, on a self-hosted
Grafana stack (Loki, Tempo, Prometheus, Grafana).

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
docker compose up -d                      # Grafana on http://localhost:3001 (admin/admin)
cp .env.example .env                      # add your ANTHROPIC_API_KEY
set -a; source .env; set +a
uv run uvicorn --factory agent.app:build_app --port 8000
curl -s localhost:8000/ask -H 'content-type: application/json' \
  -d '{"question": "What does a 2-person tent plus a canister stove cost and weigh?"}'
```

Then open Grafana, go to Explore, pick Tempo, and search for service `catalog-agent`.

## Test it

```bash
uv run pytest --cov=agent
```

Tests use a scripted fake Claude client and an in-memory span exporter. No API key needed.

## Cost

`agent/pricing.yaml` holds dated list prices. Every chat span carries `llm.cost.usd`; the
root span carries the request total. An unknown model raises instead of reporting $0.

## Roadmap

1. Agent, GenAI spans, all-in-one stack (done)
2. Token, cost, and latency metrics; spanmetrics connector
3. Dedicated Collector: prompt redaction, tail sampling, logs linked to traces
4. Dashboards and SLOs as code, burn-rate alerts
5. k6 load test at 1, 5, and 20 users: latency, 429s, and dollars per request
