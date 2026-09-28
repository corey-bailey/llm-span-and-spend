"""The dashboard is generated from code. These tests stop drift, dangling datasources,
and queries against metrics the app does not emit."""
import json
import re
from pathlib import Path

from scripts.build_dashboard import DASHBOARD_PATH, build

KNOWN_METRICS = {
    "llm_agent_request_duration_seconds_count", "llm_agent_request_duration_seconds_bucket",
    "llm_agent_request_cost_sum", "llm_agent_request_cost_count", "llm_cost_usd_total",
    "gen_ai_client_token_usage_sum", "gen_ai_client_operation_duration_seconds_bucket",
    "traces_span_metrics_calls_total", "ALERTS",
}
RULES = Path(__file__).parent.parent / "prometheus" / "rules" / "slo.yml"
PROMQL_FUNCS = {"sum", "rate", "increase", "histogram_quantile", "vector", "or", "and"}


def panels(dash):
    return [p for p in dash["panels"] if p["type"] != "row"]


def test_committed_json_matches_generator():
    assert json.loads(DASHBOARD_PATH.read_text()) == build(), \
        "run: uv run python scripts/build_dashboard.py"


def test_panel_ids_unique():
    ids = [p["id"] for p in build()["panels"]]
    assert len(ids) == len(set(ids))


def test_every_target_uses_a_provisioned_datasource():
    for p in panels(build()):
        for t in p["targets"]:
            assert t["datasource"]["uid"] in {"prometheus", "loki"}, p["title"]


def test_promql_only_references_emitted_metrics_or_recording_rules():
    recorded = set(re.findall(r"record: (\S+)", RULES.read_text()))
    for p in panels(build()):
        for t in p["targets"]:
            if t["datasource"]["uid"] != "prometheus":
                continue
            # drop label matchers, strings, ranges, and by(...) groupings: none are metric names
            expr = re.sub(r'\{[^}]*\}|"[^"]*"|\[[^\]]*\]|\b(?:by|without)\s*\([^)]*\)', "", t["expr"])
            names = set(re.findall(r"[A-Za-z_:][A-Za-z0-9_:]*", expr)) - PROMQL_FUNCS
            unknown = {n for n in names if n not in KNOWN_METRICS | recorded and not n.isdigit()}
            assert not unknown, f"{p['title']}: {unknown}"


def test_synthetic_traffic_excluded_from_every_agent_query():
    for p in panels(build()):
        for t in p["targets"]:
            if "catalog-agent" in t["expr"] and "slo:" not in t["expr"]:
                assert 'service_version!~"verify-.*"' in t["expr"], p["title"]


def test_slo_row_comes_first():
    first_row = next(p for p in build()["panels"] if p["type"] == "row")
    assert first_row["title"].startswith("SLOs")


def test_loki_filters_structured_metadata_after_the_pipe():
    # Loki stream labels here are only service_name and service_instance_id. A negative
    # matcher on any other label inside {...} matches every stream and filters nothing.
    for p in panels(build()):
        for t in p["targets"]:
            if t["datasource"]["uid"] == "loki":
                selector = t["expr"].split("}")[0]
                assert "service_version" not in selector, p["title"]
