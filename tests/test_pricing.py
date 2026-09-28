import pytest

from agent.pricing import Pricing, UnknownModelError
from tests.fakes import usage

TABLE = {
    "cache_write_multiplier": 1.25,
    "cache_read_multiplier": 0.1,
    "models": {
        "claude-sonnet-5": {"input_per_mtok": 2.0, "output_per_mtok": 10.0},
        "claude-haiku-4-5": {"input_per_mtok": 1.0, "output_per_mtok": 5.0},
    },
}


def test_cost_of_plain_tokens():
    p = Pricing(TABLE)
    # 1M in at $2 + 1M out at $10
    assert p.cost_usd("claude-sonnet-5", usage(1_000_000, 1_000_000)) == pytest.approx(12.0)


def test_cache_tokens_use_multipliers():
    p = Pricing(TABLE)
    u = usage(input_tokens=0, output_tokens=0, cache_read=1_000_000, cache_write=1_000_000)
    # read 0.1 x $1 + write 1.25 x $1
    assert p.cost_usd("claude-haiku-4-5", u) == pytest.approx(1.35)


def test_missing_cache_fields_count_as_zero():
    p = Pricing(TABLE)
    u = usage(1000, 0)
    u.cache_read_input_tokens = None
    u.cache_creation_input_tokens = None
    assert p.cost_usd("claude-sonnet-5", u) == pytest.approx(0.002)


def test_unknown_model_raises():
    with pytest.raises(UnknownModelError):
        Pricing(TABLE).cost_usd("claude-nope", usage())


def test_bundled_pricing_file_loads_and_covers_default_models():
    p = Pricing.from_file()
    assert p.cost_usd("claude-sonnet-5", usage(1_000_000, 0)) > 0
    assert p.cost_usd("claude-haiku-4-5", usage(1_000_000, 0)) > 0
