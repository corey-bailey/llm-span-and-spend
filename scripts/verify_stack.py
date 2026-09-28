"""Prove the Collector's policies against the running stack. No API spend: it drives the
real agent code with the scripted fake client and the real OTLP exporters.

    docker compose up -d
    uv run python scripts/verify_stack.py

Checks:
  1. Error traces are always kept by tail sampling.
  2. OK traces are sampled (fewer than sent reach Tempo).
  3. Captured prompt text is stripped by the Collector before storage.
  4. Every request's log line reaches Loki with the trace id that links it to Tempo.
  5. Spanmetrics counts 100% of requests, including the sampled-out ones.
"""
import os
import sys
import time
import uuid

import httpx
from opentelemetry import _logs, metrics, trace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from agent.loop import AGENT_NAME, Agent, AgentIncompleteError  # noqa: E402
from agent.pricing import Pricing  # noqa: E402
from agent.telemetry import init_logging, init_metrics, init_tracing  # noqa: E402
from tests.fakes import FakeClient, text_response  # noqa: E402

GRAFANA = os.environ.get("GRAFANA_URL", "http://localhost:3001")
AUTH = ("admin", "admin")
ERROR_TRACES = 10
OK_TRACES = 50
MARKER = "VERIFY-PROMPT-MARKER"
ANSWER_MARKER = "VERIFY-ANSWER-MARKER"
STRIPPED_KEYS = ("gen_ai.input.messages", "gen_ai.output.messages", "gen_ai.system_instructions")
SETTLE_S = 25  # tail_sampling decision_wait (10s) + batch + ingest
POLL_TIMEOUT_S = 90  # spanmetrics flushes every 60s by default


def proxy(uid: str, path: str, **params) -> dict:
    r = httpx.get(f"{GRAFANA}/api/datasources/proxy/uid/{uid}{path}", params=params, auth=AUTH, timeout=30)
    r.raise_for_status()
    return r.json()


def send_traces(version: str) -> list[str]:
    tracer = init_tracing(AGENT_NAME, version)
    agent_metrics = init_metrics(AGENT_NAME, version)
    init_logging(AGENT_NAME, version)
    pricing = Pricing.from_file()
    error_ids = []
    for _ in range(ERROR_TRACES):
        agent = Agent(FakeClient([text_response(ANSWER_MARKER, stop_reason="max_tokens")]), tracer, pricing,
                      "claude-sonnet-5", metrics=agent_metrics, capture_content=True)
        with tracer.start_as_current_span("verify") as span:
            error_ids.append(format(span.get_span_context().trace_id, "032x"))
            try:
                agent.run(f"{MARKER} question")
            except AgentIncompleteError:
                pass
    for _ in range(OK_TRACES):
        Agent(FakeClient([text_response("ok")]), tracer, pricing, "claude-sonnet-5",
              metrics=agent_metrics).run("ok question")
    trace.get_tracer_provider().force_flush()
    metrics.get_meter_provider().force_flush()
    _logs.get_logger_provider().force_flush()
    return error_ids


def poll(fetch, done, timeout_s: float = POLL_TIMEOUT_S, every_s: float = 5):
    """Call fetch() until done(value) or timeout; return the last value."""
    deadline = time.time() + timeout_s
    value = fetch()
    while not done(value) and time.time() < deadline:
        time.sleep(every_s)
        value = fetch()
    return value


def tempo_trace_ids(version: str, start: int) -> set[str]:
    # TraceQL: the tags= search form does not match resource attributes like service.version.
    found = proxy("tempo", "/api/search", q=f'{{resource.service.version="{version}"}}',
                  limit=500, start=start, end=int(time.time()) + 60)
    return {t["traceID"].rjust(32, "0") for t in found.get("traces", [])}


def span_count(trace_body: dict) -> int:
    return sum(len(ss.get("spans", [])) for b in trace_body.get("batches", [])
               for ss in b.get("scopeSpans", []))


def check(name: str, ok: bool, detail: str) -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    return ok


def main() -> int:
    version = f"verify-{uuid.uuid4().hex[:8]}"
    start = int(time.time()) - 60
    error_ids = send_traces(version)
    print(f"sent {ERROR_TRACES} error + {OK_TRACES} ok traces as service.version={version}; "
          f"waiting {SETTLE_S}s")
    time.sleep(SETTLE_S)

    stored = poll(lambda: tempo_trace_ids(version, start),
                  lambda ids: all(t in ids for t in error_ids))
    results = []
    kept_errors = [t for t in error_ids if t in stored]
    results.append(check("errors always kept", len(kept_errors) == ERROR_TRACES,
                         f"{len(kept_errors)}/{ERROR_TRACES} error traces in Tempo"))
    ok_stored = len(stored) - len(kept_errors)
    results.append(check("ok traces sampled", ok_stored < OK_TRACES,
                         f"{ok_stored}/{OK_TRACES} ok traces in Tempo"))

    trace_body = proxy("tempo", f"/api/traces/{error_ids[0]}")
    body, spans = str(trace_body), span_count(trace_body)
    leaked = [k for k in (MARKER, ANSWER_MARKER, *STRIPPED_KEYS) if k in body]
    results.append(check("prompt and answer text stripped", spans >= 2 and not leaked,
                         f"trace has {spans} spans (captured with content on); "
                         f"leaked: {leaked or 'none'}"))

    logs = proxy("loki", "/loki/api/v1/query_range",
                 query=f'{{service_name="{AGENT_NAME}"}} | trace_id = "{error_ids[0]}"',
                 start=f"{start}000000000", end=f"{int(time.time())}000000000", limit=10)
    lines = sum(len(s["values"]) for s in logs["data"]["result"])
    results.append(check("log linked to trace", lines == 1,
                         f"{lines} Loki line(s) with trace_id={error_ids[0][:12]}..."))

    q = (f'sum(traces_span_metrics_calls_total{{span_name="invoke_agent {AGENT_NAME}",'
         f'service_version="{version}"}})')
    total = ERROR_TRACES + OK_TRACES

    def spanmetric_calls() -> int:
        res = proxy("prometheus", "/api/v1/query", query=q)["data"]["result"]
        return int(float(res[0]["value"][1])) if res else 0

    calls = poll(spanmetric_calls, lambda n: n >= total)
    results.append(check("spanmetrics sees 100%", calls == total,
                         f"{calls}/{total} requests counted before sampling"))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
