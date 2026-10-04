"""Pricing table + cost estimation.

Exists specifically to compare Phase 1 (LLM + tool-use loop) vs. Phase 2 (deterministic
API calls + a single LLM synthesis call) sub-agent implementations on latency and cost —
see README.md "Phase 1 vs Phase 2". Prices are per-million-tokens, USD, as published for
the model at the time this was written — verify against https://claude.com/pricing before
relying on this for real budgeting.
"""

from __future__ import annotations

# (input $/1M tokens, output $/1M tokens)
PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


def estimate_cost_usd(model: str | None, input_tokens: int, output_tokens: int) -> float:
    if not model or model not in PRICING_PER_MTOK:
        return 0.0
    input_price, output_price = PRICING_PER_MTOK[model]
    return (input_tokens / 1_000_000) * input_price + (output_tokens / 1_000_000) * output_price
