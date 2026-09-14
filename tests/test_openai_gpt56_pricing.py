"""Regression coverage for GPT-5.6 and GPT-6 Astra pricing and model-id normalization."""

import math
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
FLEET_SCRIPTS = REPO / "skills" / "fleet-auditor" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(FLEET_SCRIPTS))

import archive_result  # noqa: E402
import measure  # noqa: E402
import shared as fleet_shared  # noqa: E402
import fleet  # noqa: E402


@pytest.fixture(autouse=True)
def _pin_gpt56_promo(monkeypatch):
    """Pin gpt-5.6-sol promo rates so tests stay regime-stable through the promo window."""
    monkeypatch.setenv("TOKEN_OPTIMIZER_PRICING_AS_OF", "2026-09-14")
    measure._apply_gpt56_sol_promo_pricing()
    fleet._apply_gpt56_sol_promo_pricing()
    archive_result._apply_gpt56_sol_promo_pricing()


@pytest.mark.parametrize(
    ("model_id", "canonical"),
    [
        ("gpt-5.6", "gpt-5.6-sol"),
        ("openrouter/openai/gpt-5.6-sol-2026-07-09", "gpt-5.6-sol"),
        ("GPT-5.6 Sol Pro", "gpt-5.6-sol"),
        ("openai:gpt-5.6-terra-2026-07-09", "gpt-5.6-terra"),
        ("gpt-5.6_luna", "gpt-5.6-luna"),
        ("gpt-5.5-pro", "gpt-5.5-pro"),
        ("gpt-6-astra", "gpt-6-astra"),
        ("openai/gpt-6-astra-2026-09-01", "gpt-6-astra"),
    ],
)
def test_gpt56_normalization_variants(model_id, canonical):
    assert measure._normalize_openai_model_name(model_id) == canonical
    assert fleet_shared.normalize_model_name(model_id) == canonical


def test_gpt56_keeps_the_existing_generic_gpt5_quality_curve():
    assert 'uncalibrated' in measure._quality_curve_for_model("gpt-5.6-sol")[0]
    assert measure._quality_curve_for_model("gpt-5.5")[0] == "openai-gpt-5.5"


def test_gpt6_astra_quality_curve_is_uncalibrated():
    assert 'uncalibrated' in measure._quality_curve_for_model("gpt-6-astra")[0]


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("gpt-5.6-sol", 1.47),
        ("gpt-5.6-terra", 0.8350),
        ("gpt-5.6-luna", 0.0835),
    ],
)
def test_gpt56_base_cost_includes_cached_and_cache_write_tokens(model, expected):
    cost = measure._get_model_cost(
        model,
        input_tokens=50_000,
        output_tokens=50_000,
        cache_read=50_000,
        cache_create=50_000,
    )
    assert math.isclose(cost, expected)


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("gpt-5.6-sol", 48.80),
        ("gpt-5.6-terra", 27.40),
        ("gpt-5.6-luna", 2.74),
    ],
)
def test_gpt56_long_context_cost_uses_documented_multiplier(model, expected):
    cost = measure._get_model_cost(
        model,
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read=1_000_000,
        cache_create=1_000_000,
    )
    assert math.isclose(cost, expected)


@pytest.mark.parametrize(
    ("model", "rate"),
    [
        ("openrouter/openai/gpt-5.6-sol-2026-07-09", 4.0),
        ("GPT-5.6 Terra", 2.0),
        ("gpt-5.6_luna", 0.20),
        ("gpt-5.6", 4.0),
    ],
)
def test_hook_savings_uses_gpt56_canonical_input_rates(monkeypatch, model, rate):
    for env_key in (
        "TOKEN_OPTIMIZER_COST_PER_MTOK",
        "CLAUDE_MODEL",
        "ANTHROPIC_MODEL",
        "CODEX_MODEL",
        "OPENAI_MODEL",
        "MODEL",
    ):
        monkeypatch.delenv(env_key, raising=False)
    monkeypatch.setenv("CODEX_MODEL", model)
    assert archive_result._estimate_savings_cost_per_mtok() == rate


@pytest.mark.parametrize(
    ("model", "input_rate", "cache_read_rate", "cache_write_rate", "output_rate"),
    [
        ("gpt-5.6-sol", 4.0, 0.40, 5.0, 20.0),
        ("gpt-5.6-terra", 2.0, 0.20, 2.50, 12.0),
        ("gpt-5.6-luna", 0.20, 0.02, 0.25, 1.20),
    ],
)
def test_fleet_uses_gpt56_canonical_rates(model, input_rate, cache_read_rate, cache_write_rate, output_rate):
    rates = fleet.DEFAULT_PRICING[model]
    assert math.isclose(rates["input"] * 1e6, input_rate)
    assert math.isclose(rates["cache_read"] * 1e6, cache_read_rate)
    assert math.isclose(rates["cache_write"] * 1e6, cache_write_rate)
    assert math.isclose(rates["output"] * 1e6, output_rate)


def test_gpt56_sol_promo_reverts_after_promo_window(monkeypatch):
    """After 2026-11-21 the gpt-5.6-sol card reverts to the standard rate."""
    monkeypatch.setenv("TOKEN_OPTIMIZER_PRICING_AS_OF", "2026-11-22")
    measure._apply_gpt56_sol_promo_pricing()
    assert measure.OPENAI_MODEL_PRICING["gpt-5.6-sol"]["input"] == 5.0
    assert measure.OPENAI_MODEL_PRICING["gpt-5.6-sol"]["output"] == 30.0
    assert measure.OPENAI_LONG_CONTEXT_PRICING["gpt-5.6-sol"]["input"] == 10.0
    fleet._apply_gpt56_sol_promo_pricing()
    assert fleet.DEFAULT_PRICING["gpt-5.6-sol"]["input"] * 1e6 == 5.0
    archive_result._apply_gpt56_sol_promo_pricing()
    assert archive_result._HOOK_INPUT_COST_PER_MTOK["gpt-5.6-sol"] == 5.0


def test_gpt6_astra_pricing_is_registered():
    rates = measure.OPENAI_MODEL_PRICING["gpt-6-astra"]
    assert rates["input"] == 10.0
    assert rates["cache_read"] == 1.0
    assert rates["cache_write"] == 12.50
    assert rates["output"] == 50.0
    lc = measure.OPENAI_LONG_CONTEXT_PRICING["gpt-6-astra"]
    assert lc["input"] == 20.0
    assert lc["cache_read"] == 2.0
    assert lc["cache_write"] == 25.0
    assert lc["output"] == 75.0
    assert fleet.DEFAULT_PRICING["gpt-6-astra"]["input"] * 1e6 == 10.0
    assert archive_result._HOOK_INPUT_COST_PER_MTOK["gpt-6-astra"] == 10.0
