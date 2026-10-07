"""Token pricing in USD per million tokens (input, output). Update when prices change."""

from __future__ import annotations

PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}
# Unknown models are priced at the most expensive known rate so budgets fail safe.
FALLBACK = (10.0, 50.0)
CACHE_READ_MULT = 0.1
CACHE_WRITE_MULT = 1.25


def cost_usd(model: str, usage: object) -> float:
    inp, out = PRICES.get(model, FALLBACK)
    g = lambda n: getattr(usage, n, 0) or 0  # noqa: E731
    tokens_in = (
        g("input_tokens")
        + g("cache_read_input_tokens") * CACHE_READ_MULT
        + g("cache_creation_input_tokens") * CACHE_WRITE_MULT
    )
    return (tokens_in * inp + g("output_tokens") * out) / 1_000_000
