import pytest

from agent.tools import TOOL_DEFINITIONS, ToolError, run_tool


def test_definitions_are_strict_and_named():
    names = {t["name"] for t in TOOL_DEFINITIONS}
    assert names == {"search_catalog", "get_product", "calculate_total"}
    for t in TOOL_DEFINITIONS:
        assert t["input_schema"]["additionalProperties"] is False


def test_search_matches_name_and_category():
    hits = run_tool("search_catalog", {"query": "sleep"})
    assert {h["sku"] for h in hits} == {"BAG-20F", "BAG-40F", "PAD-INS"}


def test_get_product_returns_one_item():
    assert run_tool("get_product", {"sku": "TENT-2P"})["price_usd"] == 289.00


def test_get_product_unknown_sku_is_tool_error():
    with pytest.raises(ToolError):
        run_tool("get_product", {"sku": "NOPE"})


def test_calculate_total_sums_price_and_weight():
    out = run_tool("calculate_total", {"skus": ["TENT-2P", "STOVE-CAN"]})
    assert out == {"skus": ["TENT-2P", "STOVE-CAN"], "total_usd": 348.00, "total_weight_g": 1435}


def test_unknown_tool_is_tool_error():
    with pytest.raises(ToolError):
        run_tool("rm_rf", {})
