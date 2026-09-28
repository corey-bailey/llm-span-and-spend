"""Turn a Messages API usage block into dollars, from a dated price table."""
from pathlib import Path

import yaml

DEFAULT_PRICING_FILE = Path(__file__).with_name("pricing.yaml")
PER_MTOK = 1_000_000


class UnknownModelError(KeyError):
    """The model has no row in the price table. Fail loudly: a silent $0 hides spend."""


class Pricing:
    def __init__(self, table: dict):
        self._models = table["models"]
        self._cache_write = table["cache_write_multiplier"]
        self._cache_read = table["cache_read_multiplier"]

    @classmethod
    def from_file(cls, path: Path = DEFAULT_PRICING_FILE) -> "Pricing":
        return cls(yaml.safe_load(path.read_text()))

    def cost_usd(self, model: str, usage) -> float:
        if model not in self._models:
            raise UnknownModelError(model)
        row = self._models[model]
        input_price = row["input_per_mtok"] / PER_MTOK
        output_price = row["output_per_mtok"] / PER_MTOK
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        return (
            usage.input_tokens * input_price
            + usage.output_tokens * output_price
            + cache_read * input_price * self._cache_read
            + cache_write * input_price * self._cache_write
        )
