"""Hermes quality must use live prompt occupancy, never cumulative totals."""
from __future__ import annotations
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import hermes_session  # noqa: E402


def _row(**overrides):
    row = {
        "id": "sess-1", "model": "gpt-5", "started_at": 1, "ended_at": 61,
        "input_tokens": 1_285_803, "output_tokens": 64_000,
        "cache_read_tokens": 0, "cache_write_tokens": 0,
        "message_count": 20, "tool_call_count": 2, "api_call_count": 129,
        "cost_status": "unknown",
    }
    row.update(overrides)
    return row


def test_cumulative_session_tokens_are_not_treated_as_occupancy():
    parsed = hermes_session.normalize_session(_row())
    assert parsed["quality"]["fill_ratio"] is None
    assert parsed["quality"]["context_source"] == "unavailable"
    assert "context_fill" in parsed["quality"]["signals_omitted"]
    assert parsed["quality"]["signal_weights"]["fill"] == 0.0


def test_provider_last_prompt_drives_occupancy_when_supplied():
    parsed = hermes_session.normalize_session(_row(), context_tokens=278_545)
    assert parsed["quality"]["fill_ratio"] == 0.2785
    assert parsed["quality"]["context_source"] == "provider_prompt_tokens"
    assert "context_fill" in parsed["quality"]["signals_active"]


def test_huge_lifetime_total_cannot_override_small_live_prompt():
    parsed = hermes_session.normalize_session(
        _row(input_tokens=20_000_000), context_tokens=100_000
    )
    assert parsed["quality"]["fill_ratio"] == 0.1


def test_missing_occupancy_renormalizes_remaining_weights():
    quality = hermes_session.compute_quality_score(
        input_tokens=10_000_000, output_tokens=500_000, message_count=20,
        context_window=1_000_000,
    )
    assert quality["signal_weights"]["message_count"] + quality["signal_weights"]["output_input_ratio"] == 1.0
    assert quality["signal_scores"]["fill"] is None
