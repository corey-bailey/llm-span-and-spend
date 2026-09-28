"""Generate the catalog-agent Grafana dashboard. Dashboards as code: edit this file,
not the JSON. Run: uv run python scripts/build_dashboard.py"""
import json
from pathlib import Path

DASHBOARD_PATH = Path(__file__).parent.parent / "grafana" / "dashboards" / "catalog-agent.json"
SEL = 'service_name="catalog-agent",service_version!~"verify-.*"'
PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}
GRID_WIDTH = 24

REQ_COUNT = f"llm_agent_request_duration_seconds_count{{{SEL}}}"
REQ_ERRORS = f'llm_agent_request_duration_seconds_count{{{SEL},outcome!="ok"}}'
REQ_BUCKET = f"llm_agent_request_duration_seconds_bucket{{{SEL}}}"
FAST_BUCKET = f'llm_agent_request_duration_seconds_bucket{{{SEL},le="20.48"}}'


def prom(expr: str, legend: str = "", instant: bool = False) -> dict:
    t = {"datasource": PROM, "expr": expr, "legendFormat": legend or "__auto", "refId": ""}
    if instant:
        t.update(instant=True, range=False, format="table")
    return t


def stat(title, expr, unit, thresholds=None, decimals=None, desc=""):
    steps = [{"color": c, "value": v} for v, c in (thresholds or [(None, "text")])]
    defaults = {"unit": unit, "thresholds": {"mode": "absolute", "steps": steps}}
    if decimals is not None:
        defaults["decimals"] = decimals
    return {"type": "stat", "title": title, "description": desc, "datasource": PROM,
            "targets": [prom(expr)], "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {"colorMode": "value", "graphMode": "area", "reduceOptions":
                        {"calcs": ["lastNotNull"], "fields": "", "values": False}},
            "_size": (4, 4)}


def series(title, targets, unit, desc="", exemplars=False, stack=False, w=12):
    targets = [{**t, "exemplar": exemplars} for t in targets]
    custom = {"lineWidth": 2, "fillOpacity": 15 if stack else 0,
              "stacking": {"mode": "normal" if stack else "none"}}
    return {"type": "timeseries", "title": title, "description": desc, "datasource": PROM,
            "targets": targets, "fieldConfig": {"defaults": {"unit": unit, "custom": custom},
                                                "overrides": []},
            "options": {"legend": {"displayMode": "list", "placement": "bottom"},
                        "tooltip": {"mode": "multi"}},
            "_size": (w, 8)}


def row(title):
    return {"type": "row", "title": title, "collapsed": False, "panels": [], "_size": (GRID_WIDTH, 1)}


def ratio_28d(num: str, den: str) -> str:
    return f"(sum(increase({num}[28d])) or vector(0)) / sum(increase({den}[28d]))"


def panels() -> list[dict]:
    return [
        row("SLOs (28 days): 99% succeed, 95% finish in 20.48s"),
        stat("Availability", f"1 - ({ratio_28d(REQ_ERRORS, REQ_COUNT)})", "percentunit",
             [(None, "red"), (0.99, "green")], 2, "Share of requests with outcome=ok. Target 99%."),
        stat("Error budget left", f"1 - ({ratio_28d(REQ_ERRORS, REQ_COUNT)}) / 0.01", "percentunit",
             [(None, "red"), (0.25, "orange"), (0.5, "green")], 0,
             "Share of the 1% error budget not yet spent this window."),
        stat("Within 20.48s", f"sum(increase({FAST_BUCKET}[28d])) / sum(increase({REQ_COUNT}[28d]))",
             "percentunit", [(None, "red"), (0.95, "green")], 1, "Latency SLO. Target 95%."),
        stat("Burn rate (1h)", "slo:agent_errors:ratio_rate1h / 0.01", "x",
             [(None, "green"), (6, "orange"), (14.4, "red")], 1,
             "1x spends the budget exactly over 28 days. 14.4x pages, 6x opens a ticket."),
        stat("Spend (24h)", f"sum(increase(llm_cost_usd_total{{{SEL}}}[24h]))", "currencyUSD",
             decimals=2, desc="List-price model spend, including billed-but-failed requests."),
        stat("Cost per request (1h)",
             f"sum(rate(llm_agent_request_cost_sum{{{SEL}}}[1h])) / sum(rate(llm_agent_request_cost_count{{{SEL}}}[1h]))",
             "currencyUSD", decimals=4),
        {"type": "table", "title": "Firing alerts", "datasource": PROM,
         "targets": [prom('ALERTS{alertstate="firing"}', instant=True)],
         "fieldConfig": {"defaults": {}, "overrides": []},
         "transformations": [{"id": "organize", "options": {"excludeByName":
                              {"Time": True, "Value": True, "__name__": True, "alertstate": True}}}],
         "_size": (GRID_WIDTH, 4)},

        row("Traffic and latency"),
        series("Requests per second by outcome",
               [prom(f"sum by (outcome) (rate({REQ_COUNT}[5m]))", "{{outcome}}")], "reqps", stack=True),
        series("Request latency (exemplars link to traces)",
               [prom(f"histogram_quantile({q}, sum by (le) (rate({REQ_BUCKET}[5m])))", f"p{int(q*100)}")
                for q in (0.5, 0.95, 0.99)], "s", exemplars=True),
        series("Model call latency p95 by model",
               [prom("histogram_quantile(0.95, sum by (le, gen_ai_request_model) "
                     f"(rate(gen_ai_client_operation_duration_seconds_bucket{{{SEL}}}[5m])))",
                     "{{gen_ai_request_model}}")], "s"),
        series("Error-budget burn rate",
               [prom("slo:agent_errors:ratio_rate1h / 0.01", "availability 1h"),
                prom("slo:agent_errors:ratio_rate6h / 0.01", "availability 6h"),
                prom("slo:agent_slow:ratio_rate1h / 0.05", "latency 1h")], "x",
               "Page at 14.4x (1h and 5m), ticket at 6x (6h and 30m)."),

        row("Cost and tokens"),
        series("Spend rate ($ per hour)",
               [prom(f"sum(rate(llm_cost_usd_total{{{SEL}}}[5m])) * 3600", "$/hour")], "currencyUSD"),
        series("Tokens per second",
               [prom(f"sum by (gen_ai_token_type) (rate(gen_ai_client_token_usage_sum{{{SEL}}}[5m]))",
                     "{{gen_ai_token_type}}")], "short", stack=True),

        row("Agent behavior (from span metrics, 100% of spans before sampling)"),
        series("Model calls per request",
               [prom(f'sum(rate(traces_span_metrics_calls_total{{{SEL},span_name=~"chat .*"}}[5m])) / '
                     f'sum(rate(traces_span_metrics_calls_total{{{SEL},span_name=~"invoke_agent.*"}}[5m]))',
                     "calls/request")], "short",
               "More than 2 usually means the agent is retrying tools. Each extra call is paid for."),
        series("Tool calls per second by tool",
               [prom(f'sum by (span_name) (rate(traces_span_metrics_calls_total{{{SEL},'
                     f'span_name=~"execute_tool .*"}}[5m]))', "{{span_name}}")], "reqps"),
        {"type": "logs", "title": "Failed requests (click a line for its trace)", "datasource": LOKI,
         "targets": [{"datasource": LOKI, "refId": "",
                      # service_version is structured metadata, not a stream label, so it is
                      # filtered after the pipe. In the selector, != on a missing label matches all.
                      "expr": '{service_name="catalog-agent"} | service_version!~"verify-.*" '
                              '| severity_text="ERROR"'}],
         "options": {"showTime": True, "wrapLogMessage": True, "enableLogDetails": True},
         "_size": (GRID_WIDTH, 8)},
    ]


def layout(items: list[dict]) -> list[dict]:
    """Place panels left to right, wrapping at the grid width; assign ids and refIds."""
    out, x, y, row_h = [], 0, 0, 0
    for i, item in enumerate(items, start=1):
        w, h = item["_size"]
        if x + w > GRID_WIDTH:
            x, y, row_h = 0, y + row_h, 0
        panel = {k: v for k, v in item.items() if k != "_size"}
        panel["id"] = i
        panel["gridPos"] = {"x": x, "y": y, "w": w, "h": h}
        if "targets" in panel:
            panel["targets"] = [{**t, "refId": chr(ord("A") + n)} for n, t in enumerate(panel["targets"])]
        out.append(panel)
        x, row_h = x + w, max(row_h, h)
    return out


def build() -> dict:
    return {
        "uid": "catalog-agent",
        "title": "Catalog agent: SLOs, cost, and behavior",
        "tags": ["llm", "opentelemetry", "slo"],
        "timezone": "browser",
        "schemaVersion": 41,
        "version": 1,
        "editable": True,
        "refresh": "30s",
        "time": {"from": "now-6h", "to": "now"},
        "panels": layout(panels()),
    }


if __name__ == "__main__":
    DASHBOARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    DASHBOARD_PATH.write_text(json.dumps(build(), indent=2) + "\n")
    print(f"wrote {DASHBOARD_PATH}")
