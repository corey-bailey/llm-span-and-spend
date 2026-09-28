"""Three catalog tools the agent can call. Plain functions: no telemetry here."""
import json
from pathlib import Path

CATALOG = json.loads((Path(__file__).parent.parent / "data" / "catalog.json").read_text())
BY_SKU = {item["sku"]: item for item in CATALOG}


class ToolError(Exception):
    """A tool failed in a way the model should see and recover from."""


def _schema(properties: dict) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


TOOL_DEFINITIONS = [
    {
        "name": "search_catalog",
        "description": "Find catalog items whose name or category contains the query text.",
        "strict": True,
        "input_schema": _schema({"query": {"type": "string"}}),
    },
    {
        "name": "get_product",
        "description": "Get one catalog item by SKU, including price and weight.",
        "strict": True,
        "input_schema": _schema({"sku": {"type": "string"}}),
    },
    {
        "name": "calculate_total",
        "description": "Total price (USD) and weight (grams) for a list of SKUs.",
        "strict": True,
        "input_schema": _schema({"skus": {"type": "array", "items": {"type": "string"}}}),
    },
]


def _search(query: str) -> list[dict]:
    q = query.lower()
    return [i for i in CATALOG if q in i["name"].lower() or q in i["category"].lower()]


def _get(sku: str) -> dict:
    if sku not in BY_SKU:
        raise ToolError(f"unknown sku: {sku}")
    return BY_SKU[sku]


def _total(skus: list[str]) -> dict:
    items = [_get(s) for s in skus]
    return {
        "skus": skus,
        "total_usd": round(sum(i["price_usd"] for i in items), 2),
        "total_weight_g": sum(i["weight_g"] for i in items),
    }


HANDLERS = {
    "search_catalog": lambda a: _search(a["query"]),
    "get_product": lambda a: _get(a["sku"]),
    "calculate_total": lambda a: _total(a["skus"]),
}


def run_tool(name: str, args: dict):
    if name not in HANDLERS:
        raise ToolError(f"unknown tool: {name}")
    return HANDLERS[name](args)
