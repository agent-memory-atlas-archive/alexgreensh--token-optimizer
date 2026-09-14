"""Correctness gauntlet for the INTEGRATED #175 extraction.

Dimension: CORRECTNESS. Attack the numbers.

Every test in this file targets a specific numeric correctness gap in the
merged #175 code (delta token accounting, per-request long-context pricing,
pricing values, codex_log_index totals, resolve_session identity).  Tests
that expose a real bug are written to FAIL; surfaces that are clean are
asserted as passing guards so a future regression is caught.

Run: python3 -m pytest tests/test_175gaunt_correctness.py -v
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import codex_session as cs  # noqa: E402
import codex_log_index as index  # noqa: E402
import measure as m  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SID_A = "aaaaaaaa-1111-1111-1111-111111111111"
SID_B = "bbbbbbbb-2222-2222-2222-222222222222"


def _tc(usage_cum, usage_last=None):
    """Build a token_count event_msg record."""
    info = {"total_token_usage": usage_cum}
    if usage_last is not None:
        info["last_token_usage"] = usage_last
    return {"type": "event_msg", "payload": {"type": "token_count", "info": info}}


def _agent_msg(text="hello"):
    return {"type": "event_msg", "payload": {"type": "agent_message", "message": text}}


def _meta(sid, model="gpt-5.4"):
    return {"type": "session_meta", "payload": {"id": sid, "cwd": "/project"}}


def _turn_ctx(model="gpt-5.4"):
    return {"type": "turn_context", "payload": {"model": model}}


def write_session(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(map(json.dumps, records)) + "\n", encoding="utf-8")
    return path


# ===========================================================================
# 1. DELTA TOKEN ACCOUNTING (cumulative -> delta)
# ===========================================================================

class TestDeltaTokenAccounting:
    """The integrator FLAGGED a known over-count: in sampled=True mode the
    first token_count record counts full cumulative usage instead of
    last_token_usage."""

    def test_first_record_uses_cumulative_instead_of_last_token_usage(self, tmp_path):
        """CRITICAL: first token_count record over-counts.

        When previous_usage is None (first token_count in the stream), the
        delta logic falls to ``else: turn_usage = usage`` which is the FULL
        cumulative total, not the per-request ``last_token_usage`` that was
        already computed.  This over-counts whenever the first record's
        cumulative usage exceeds its last_token_usage, which happens in:

        * Resumed sessions (cumulative carries over from a prior session).
        * sampled=True tail mode (first record seen is mid-session).

        Repro: two API calls where the first record's cumulative != last.
        """
        records = [
            _meta(SID_A),
            _turn_ctx(),
            _agent_msg(),
            # First API call: cumulative already at 500 (resumed session),
            # but THIS call only used 100 input / 20 output.
            _tc(
                usage_cum={"input_tokens": 500, "cached_input_tokens": 200,
                           "output_tokens": 100, "reasoning_output_tokens": 10},
                usage_last={"input_tokens": 100, "cached_input_tokens": 50,
                            "output_tokens": 20, "reasoning_output_tokens": 5},
            ),
            _agent_msg(),
            # Second API call: cumulative 700, last 200/40.
            _tc(
                usage_cum={"input_tokens": 700, "cached_input_tokens": 300,
                           "output_tokens": 140, "reasoning_output_tokens": 14},
                usage_last={"input_tokens": 200, "cached_input_tokens": 100,
                            "output_tokens": 40, "reasoning_output_tokens": 4},
            ),
        ]
        p = write_session(tmp_path / "session.jsonl", records)
        parsed = cs.parse_session_jsonl(p)

        # Correct totals (using last_token_usage for first turn):
        #   turn 1: fresh=50,  cache_read=50,  output=20
        #   turn 2: fresh=100, cache_read=100, output=40
        #   total_input  = 150 + 150 = 300
        #   total_output = 60
        #
        # Buggy totals (using cumulative for first turn):
        #   turn 1: fresh=300, cache_read=200, output=100
        #   turn 2: fresh=100, cache_read=100, output=40
        #   total_input  = 400 + 300 = 700  (over-counted by 400)
        #   total_output = 140                (over-counted by 80)
        assert parsed["total_input_tokens"] == 300, (
            f"Expected 300 (last_token_usage based), got {parsed['total_input_tokens']} "
            f"(cumulative over-count: +{parsed['total_input_tokens'] - 300})"
        )
        assert parsed["total_output_tokens"] == 60, (
            f"Expected 60, got {parsed['total_output_tokens']}"
        )

    def test_over_count_bound_is_first_record_cumulative_minus_last(self, tmp_path):
        """CRITICAL: bound the over-count.

        The over-count equals exactly (cumulative - last) of the first
        token_count record.  In sampled tail mode this can be the ENTIRE
        pre-sampling usage, which is unbounded relative to the visible tail.
        """
        records = [
            _meta(SID_A),
            _turn_ctx(),
            _agent_msg(),
            _tc(
                usage_cum={"input_tokens": 10_000, "cached_input_tokens": 4_000,
                           "output_tokens": 2_000, "reasoning_output_tokens": 200},
                usage_last={"input_tokens": 100, "cached_input_tokens": 40,
                            "output_tokens": 20, "reasoning_output_tokens": 2},
            ),
            _agent_msg(),
            _tc(
                usage_cum={"input_tokens": 10_100, "cached_input_tokens": 4_040,
                           "output_tokens": 2_020, "reasoning_output_tokens": 202},
                usage_last={"input_tokens": 100, "cached_input_tokens": 40,
                            "output_tokens": 20, "reasoning_output_tokens": 2},
            ),
        ]
        p = write_session(tmp_path / "session.jsonl", records)
        parsed = cs.parse_session_jsonl(p)

        # Correct: 2 turns * (fresh=60 + cache_read=40 + output=20) = 200 input, 40 output
        correct_input = 200
        correct_output = 40
        actual_input = parsed["total_input_tokens"]
        actual_output = parsed["total_output_tokens"]
        over_count_input = actual_input - correct_input
        over_count_output = actual_output - correct_output

        # Fixed: the first record counts last_token_usage, so there is no
        # over-count. (Pre-fix bound documented here was exactly
        # cumulative_first - last_first = 9900, the entire pre-tail usage.)
        assert over_count_input == 0, (
            f"Over-count {over_count_input} != 0: the first record must count "
            f"last_token_usage, not the full cumulative total"
        )
        assert actual_input == correct_input, (
            f"Expected {correct_input}, got {actual_input}"
        )
        assert actual_output == correct_output, (
            f"Expected {correct_output}, got {actual_output}"
        )

    def test_sampled_parameter_is_accepted_but_never_used(self):
        """CRITICAL: the ``sampled`` kwarg is dead code.

        _parse_session_records accepts sampled=False but never references it.
        The call site on line 340 passes sampled=True, but the function
        body has no branch that reads it.  This means the sampled tail path
        gets the same (buggy) first-record treatment as the full path.
        """
        import inspect

        src = inspect.getsource(cs._parse_session_records)
        # The parameter is in the signature...
        assert "sampled" in src
        # ...but it is never READ in the body (only appears in the def line).
        body_lines = [
            line for line in src.splitlines()
            if "sampled" in line and not line.strip().startswith("def ")
        ]
        assert body_lines == [], (
            f"sampled is referenced in body: {body_lines}"
        )

    def test_cached_input_not_double_counted_in_normal_delta(self, tmp_path):
        """Guard: cached_input_tokens is an inclusive subset of input_tokens.
        fresh_input = input - cached, cache_read = cached.  No double-count."""
        records = [
            _meta(SID_A),
            _turn_ctx(),
            _agent_msg(),
            _tc(usage_cum={"input_tokens": 100, "cached_input_tokens": 50,
                           "output_tokens": 20, "reasoning_output_tokens": 2},
                usage_last={"input_tokens": 100, "cached_input_tokens": 50,
                            "output_tokens": 20, "reasoning_output_tokens": 2}),
            _agent_msg(),
            _tc(usage_cum={"input_tokens": 300, "cached_input_tokens": 150,
                           "output_tokens": 60, "reasoning_output_tokens": 6},
                usage_last={"input_tokens": 200, "cached_input_tokens": 100,
                            "output_tokens": 40, "reasoning_output_tokens": 4}),
        ]
        p = write_session(tmp_path / "session.jsonl", records)
        parsed = cs.parse_session_jsonl(p)
        # Turn 1: cumulative == last (first call), so turn_usage = cumulative = 100/50/20
        # Turn 2: delta = 200/100/40
        # fresh = 50 + 100 = 150, cache_read = 50 + 100 = 150
        # total_input = 300, total_output = 60
        assert parsed["total_input_tokens"] == 300
        assert parsed["total_output_tokens"] == 60
        assert parsed["total_cache_read"] == 150

    def test_reasoning_output_tokens_not_double_counted(self, tmp_path):
        """Guard: reasoning_output_tokens is computed in the delta dict but
        never added to the output bucket.  If output_tokens already includes
        reasoning (OpenAI semantics), this is correct.  Verify the output
        total equals output_tokens delta only, not output + reasoning."""
        records = [
            _meta(SID_A),
            _turn_ctx(),
            _agent_msg(),
            _tc(usage_cum={"input_tokens": 100, "cached_input_tokens": 50,
                           "output_tokens": 20, "reasoning_output_tokens": 10},
                usage_last={"input_tokens": 100, "cached_input_tokens": 50,
                            "output_tokens": 20, "reasoning_output_tokens": 10}),
        ]
        p = write_session(tmp_path / "session.jsonl", records)
        parsed = cs.parse_session_jsonl(p)
        # output_tokens=20 includes reasoning=10; total should be 20, not 30
        assert parsed["total_output_tokens"] == 20, (
            "reasoning_output_tokens should NOT be added on top of output_tokens"
        )

    def test_cumulative_decrease_no_last_does_not_over_count(self, tmp_path):
        """CRITICAL: cumulative token usage DECREASES mid-stream (compaction
        reset, session resume boundary) with no last_token_usage.

        Before the fix, the ``elif not turn_usage`` fallback attributed the
        FULL cumulative total to one call, over-counting by the entire
        pre-reset total.  After the fix, the decrease record is skipped
        (contributes 0) and previous_usage stays at the pre-decrease baseline
        so the next delta is correct.

        Sequence: 1000 -> 1500 -> 1200(decrease) -> 1700
        Without last_token_usage:
          r1: first, no last -> full cum = 1000. prev=1000.
          r2: 1500>=1000 -> delta=500. prev=1500.
          r3: 1200<1500 -> decrease, no last -> skip(0). prev stays 1500.
          r4: 1700>=1500 -> delta=200. prev=1700.
          Total = 1000+500+0+200 = 1700
        Before fix: r3 added full 1200, total = 3200.
        """
        records = [
            _meta(SID_A),
            _turn_ctx(),
            _agent_msg(),
            _tc(usage_cum={"input_tokens": 1000, "cached_input_tokens": 400,
                           "output_tokens": 100, "reasoning_output_tokens": 10}),
            _agent_msg(),
            _tc(usage_cum={"input_tokens": 1500, "cached_input_tokens": 600,
                           "output_tokens": 150, "reasoning_output_tokens": 15}),
            _agent_msg(),
            _tc(usage_cum={"input_tokens": 1200, "cached_input_tokens": 500,
                           "output_tokens": 120, "reasoning_output_tokens": 12}),  # DECREASE
            _agent_msg(),
            _tc(usage_cum={"input_tokens": 1700, "cached_input_tokens": 700,
                           "output_tokens": 170, "reasoning_output_tokens": 17}),
        ]
        p = write_session(tmp_path / "decrease.jsonl", records)
        parsed = cs.parse_session_jsonl(p)
        assert parsed["total_input_tokens"] == 1700, (
            f"cumulative-decrease over-count: got {parsed['total_input_tokens']}, "
            "expected 1700 (was 3200 before fix)"
        )
        assert parsed["total_output_tokens"] == 170, (
            f"output over-count: got {parsed['total_output_tokens']}, expected 170"
        )

    def test_cumulative_decrease_with_last_uses_per_request(self, tmp_path):
        """CRITICAL: cumulative DECREASES but last_token_usage IS present.

        The per-request value is correct and should be used, not the delta
        against the (now stale) previous_usage.

        Sequence: 1000/last=1000 -> 1500/last=500 -> 1200/last=300 -> 1700/last=500
          r1: last=1000. r2: delta=500. r3: decrease, last=300. r4: delta=200.
          Total = 1000+500+300+200 = 2000
        """
        records = [
            _meta(SID_A),
            _turn_ctx(),
            _agent_msg(),
            _tc(usage_cum={"input_tokens": 1000, "cached_input_tokens": 400,
                           "output_tokens": 100, "reasoning_output_tokens": 10},
                usage_last={"input_tokens": 1000, "cached_input_tokens": 400,
                            "output_tokens": 100, "reasoning_output_tokens": 10}),
            _agent_msg(),
            _tc(usage_cum={"input_tokens": 1500, "cached_input_tokens": 600,
                           "output_tokens": 150, "reasoning_output_tokens": 15},
                usage_last={"input_tokens": 500, "cached_input_tokens": 200,
                            "output_tokens": 50, "reasoning_output_tokens": 5}),
            _agent_msg(),
            _tc(usage_cum={"input_tokens": 1200, "cached_input_tokens": 500,
                           "output_tokens": 120, "reasoning_output_tokens": 12},
                usage_last={"input_tokens": 300, "cached_input_tokens": 100,
                            "output_tokens": 30, "reasoning_output_tokens": 3}),
            _agent_msg(),
            _tc(usage_cum={"input_tokens": 1700, "cached_input_tokens": 700,
                           "output_tokens": 170, "reasoning_output_tokens": 17},
                usage_last={"input_tokens": 500, "cached_input_tokens": 200,
                            "output_tokens": 50, "reasoning_output_tokens": 5}),
        ]
        p = write_session(tmp_path / "decrease_with_last.jsonl", records)
        parsed = cs.parse_session_jsonl(p)
        assert parsed["total_input_tokens"] == 2000, (
            f"cumulative-decrease with last: got {parsed['total_input_tokens']}, "
            "expected 2000"
        )

    def test_negative_token_fields_clamped(self, tmp_path):
        """MEDIUM: corrupt/adversarial negative token fields must not drive
        cache_read, output, or cache_hit_rate negative."""
        records = [
            _meta(SID_A),
            _turn_ctx(),
            _agent_msg(),
            _tc(usage_cum={"input_tokens": 100, "cached_input_tokens": -30,
                           "output_tokens": -50, "reasoning_output_tokens": -10}),
        ]
        p = write_session(tmp_path / "negative.jsonl", records)
        parsed = cs.parse_session_jsonl(p)
        assert parsed["total_cache_read"] >= 0, (
            f"negative cache_read not clamped: {parsed['total_cache_read']}"
        )
        assert parsed["total_output_tokens"] >= 0, (
            f"negative output not clamped: {parsed['total_output_tokens']}"
        )
        assert 0.0 <= parsed["cache_hit_rate"] <= 1.0, (
            f"cache_hit_rate out of [0,1]: {parsed['cache_hit_rate']}"
        )


# ===========================================================================
# 2. PER-REQUEST LONG-CONTEXT PRICING
# ===========================================================================

class TestPerRequestLongContextPricing:
    """_cost_from_model_breakdown recurses over requests[] -- does each
    request get the right short/long rate?"""

    def test_mixed_short_and_long_requests_each_get_correct_rate(self):
        """HIGH: a session with both short and long requests must price each
        at its own rate, not the session aggregate rate."""
        # Short request: fresh=100K + cache_read=100K = 200K total input < 272K threshold
        short_req = {"fresh_input": 100_000, "cache_read": 100_000, "output": 1_000}
        # Long request: fresh=200K + cache_read=200K = 400K total input > 272K threshold
        long_req = {"fresh_input": 200_000, "cache_read": 200_000, "output": 1_000}

        parts = {
            "fresh_input": 300_000,
            "cache_read": 300_000,
            "output": 2_000,
            "cache_create": 0,
            "requests": [short_req, long_req],
        }
        cost = m._cost_from_model_breakdown({"gpt-6-astra": parts})

        # Short: 100K*10/1M + 1K*50/1M + 100K*1/1M = 1.0 + 0.05 + 0.1 = 1.15
        short_cost = 1.15
        # Long:  200K*20/1M + 1K*75/1M + 200K*2/1M = 4.0 + 0.075 + 0.4 = 4.475
        long_cost = 4.475
        expected = short_cost + long_cost  # 5.625

        assert cost == pytest.approx(expected, rel=1e-4), (
            f"Mixed short+long cost {cost} != expected {expected}"
        )

    def test_session_aggregate_above_threshold_but_requests_below(self):
        """HIGH: if the session SUM exceeds the threshold but each individual
        request is below it, each request must still get the SHORT rate.

        This is the core correctness property: long-context pricing applies
        per request, never to a session sum.
        """
        # Three short requests, each 100K input (below 272K threshold)
        # but session total = 300K (above threshold)
        short_req = {"fresh_input": 50_000, "cache_read": 50_000, "output": 1_000}
        parts = {
            "fresh_input": 150_000,
            "cache_read": 150_000,
            "output": 3_000,
            "cache_create": 0,
            "requests": [short_req] * 3,
        }
        cost = m._cost_from_model_breakdown({"gpt-6-astra": parts})

        # Each request: 50K*10/1M + 1K*50/1M + 50K*1/1M = 0.5 + 0.05 + 0.05 = 0.6
        # 3 requests = 1.8 (all at SHORT rate, despite session sum 300K > 272K)
        per_req = 0.6
        expected = per_req * 3  # 1.8

        assert cost == pytest.approx(expected, rel=1e-4), (
            f"Per-request short rate expected {expected}, got {cost}. "
            "Session aggregate must NOT trigger long-context pricing."
        )

    def test_request_at_exact_threshold_boundary(self):
        """MEDIUM: the threshold is > (strict), not >=.  A request with
        full_input == 272_000 should get the SHORT rate."""
        # fresh + cache_read = 272_000 exactly
        boundary_req = {"fresh_input": 136_000, "cache_read": 136_000, "output": 0}
        parts = {
            "fresh_input": 136_000,
            "cache_read": 136_000,
            "output": 0,
            "cache_create": 0,
            "requests": [boundary_req],
        }
        cost = m._cost_from_model_breakdown({"gpt-6-astra": parts})
        # Short rate: 136K*10/1M + 0 + 136K*1/1M = 1.36 + 0.136 = 1.496
        short_expected = 1.496
        # Long rate:  136K*20/1M + 0 + 136K*2/1M = 2.72 + 0.272 = 2.992
        long_expected = 2.992
        assert cost == pytest.approx(short_expected, rel=1e-4), (
            f"At exact threshold (272_000), expected short rate {short_expected}, "
            f"got {cost} (long would be {long_expected})"
        )

    def test_request_one_token_above_threshold_gets_long_rate(self):
        """MEDIUM: full_input = 272_001 should get the LONG rate."""
        over_req = {"fresh_input": 136_000, "cache_read": 136_001, "output": 0}
        parts = {
            "fresh_input": 136_000,
            "cache_read": 136_001,
            "output": 0,
            "cache_create": 0,
            "requests": [over_req],
        }
        cost = m._cost_from_model_breakdown({"gpt-6-astra": parts})
        # Long: 136K*20/1M + 136001*2/1M = 2.72 + 0.272002 = 2.992002
        long_expected = 2.992002
        assert cost == pytest.approx(long_expected, rel=1e-4), (
            f"Above threshold (272_001), expected long rate {long_expected}, got {cost}"
        )

    def test_empty_requests_list_falls_back_to_aggregate(self):
        """Guard: an empty requests list should fall through to aggregate
        pricing (the ``if requests:`` guard is falsy for [])."""
        parts = {
            "fresh_input": 100_000,
            "cache_read": 100_000,
            "output": 1_000,
            "cache_create": 0,
            "requests": [],
        }
        cost = m._cost_from_model_breakdown({"gpt-6-astra": parts})
        # Aggregate: 100K*10/1M + 1K*50/1M + 100K*1/1M = 1.15
        assert cost == pytest.approx(1.15, rel=1e-4)

    def test_cache_read_dropped_when_request_has_zero_input_and_output(self, tmp_path):
        """MEDIUM: when a turn has cached_input > 0 but input == 0 and
        output == 0, the request is NOT appended to requests[], but
        cache_read IS added to the bucket.  When other requests exist,
        _cost_from_model_breakdown prices only the requests and silently
        drops those cache_read tokens from the cost."""
        # Construct a session where:
        # Turn 1: normal request (input=100, cached=50, output=20)
        # Turn 2: cumulative unchanged except cached reclassified
        #   (input_delta=0, cached_delta=10, output_delta=0)
        #   -> fresh_input_delta=0, cache_read_delta=10, no request appended
        records = [
            _meta(SID_A),
            _turn_ctx(),
            _agent_msg(),
            _tc(usage_cum={"input_tokens": 100, "cached_input_tokens": 50,
                           "output_tokens": 20, "reasoning_output_tokens": 0},
                usage_last={"input_tokens": 100, "cached_input_tokens": 50,
                            "output_tokens": 20, "reasoning_output_tokens": 0}),
            _agent_msg(),
            # Cumulative: input unchanged (100), cached grew (60), output unchanged (20)
            _tc(usage_cum={"input_tokens": 100, "cached_input_tokens": 60,
                           "output_tokens": 20, "reasoning_output_tokens": 0},
                usage_last={"input_tokens": 0, "cached_input_tokens": 10,
                            "output_tokens": 0, "reasoning_output_tokens": 0}),
        ]
        p = write_session(tmp_path / "session.jsonl", records)
        parsed = cs.parse_session_jsonl(p)

        breakdown = parsed["model_usage_breakdown"]["gpt-5.4"]
        # Bucket sums include the 10 cache_read from turn 2
        assert breakdown["cache_read"] == 60, (
            f"Expected cache_read=60 (50+10), got {breakdown['cache_read']}"
        )
        # But requests only has 1 entry (turn 2 was skipped)
        requests = breakdown.get("requests", [])
        assert len(requests) == 1, (
            f"Expected 1 request (turn 2 skipped), got {len(requests)}"
        )
        # The cost via requests drops the 10 cache_read from turn 2
        cost_via_requests = m._cost_from_model_breakdown({"gpt-5.4": breakdown})
        # Cost should account for cache_read=60, but only prices cache_read=50
        cost_with_full_cache = m._get_model_cost(
            "gpt-5.4",
            input_tokens=50,  # fresh
            output_tokens=20,
            cache_read=60,    # full cache_read
            cache_create=0,
        )
        # This assertion FAILS if the 10 cache_read tokens are dropped
        assert cost_via_requests == pytest.approx(cost_with_full_cache, rel=1e-6), (
            f"Cost via requests ({cost_via_requests}) drops cache_read tokens "
            f"that exist in the bucket. Full cache cost = {cost_with_full_cache}"
        )


# ===========================================================================
# 3. PRICING VALUES
# ===========================================================================

class TestPricingValues:
    """gpt-6-astra + gpt-5.6-sol short/long match the sheet, and the Sol PROMO
    price reverts after 2026-11-21 via the dated mechanism."""

    def test_gpt6_astra_short_context_rates(self):
        """gpt-6-astra standard (short-context) rates."""
        rates = m.OPENAI_MODEL_PRICING["gpt-6-astra"]
        assert rates["input"] == 10.0
        assert rates["cache_read"] == 1.0
        assert rates["cache_write"] == 12.50
        assert rates["output"] == 50.0

    def test_gpt6_astra_long_context_rates(self):
        """gpt-6-astra long-context rates (2x short for input/cache_read/cache_write, 1.5x output)."""
        rates = m.OPENAI_LONG_CONTEXT_PRICING["gpt-6-astra"]
        assert rates["input"] == 20.0
        assert rates["cache_read"] == 2.0
        assert rates["cache_write"] == 25.0
        assert rates["output"] == 75.0

    def test_gpt56_sol_standard_short_context_rates(self, monkeypatch):
        """gpt-5.6-sol STANDARD (post-promo) short-context rates."""
        monkeypatch.setenv("TOKEN_OPTIMIZER_PRICING_AS_OF", "2026-11-22")
        m._apply_gpt56_sol_promo_pricing()
        rates = m.OPENAI_MODEL_PRICING["gpt-5.6-sol"]
        assert rates["input"] == 5.0
        assert rates["cache_read"] == 0.50
        assert rates["cache_write"] == 6.25
        assert rates["output"] == 30.0

    def test_gpt56_sol_standard_long_context_rates(self, monkeypatch):
        """gpt-5.6-sol STANDARD (post-promo) long-context rates."""
        monkeypatch.setenv("TOKEN_OPTIMIZER_PRICING_AS_OF", "2026-11-22")
        m._apply_gpt56_sol_promo_pricing()
        rates = m.OPENAI_LONG_CONTEXT_PRICING["gpt-5.6-sol"]
        assert rates["input"] == 10.0
        assert rates["cache_read"] == 1.0
        assert rates["cache_write"] == 12.50
        assert rates["output"] == 45.0

    def test_gpt56_sol_promo_short_context_rates(self, monkeypatch):
        """gpt-5.6-sol PROMO short-context rates (active before 2026-11-21)."""
        monkeypatch.setenv("TOKEN_OPTIMIZER_PRICING_AS_OF", "2026-09-14")
        m._apply_gpt56_sol_promo_pricing()
        rates = m.OPENAI_MODEL_PRICING["gpt-5.6-sol"]
        assert rates["input"] == 4.0
        assert rates["cache_read"] == 0.40
        assert rates["cache_write"] == 5.0
        assert rates["output"] == 20.0

    def test_gpt56_sol_promo_long_context_rates(self, monkeypatch):
        """gpt-5.6-sol PROMO long-context rates."""
        monkeypatch.setenv("TOKEN_OPTIMIZER_PRICING_AS_OF", "2026-09-14")
        m._apply_gpt56_sol_promo_pricing()
        rates = m.OPENAI_LONG_CONTEXT_PRICING["gpt-5.6-sol"]
        assert rates["input"] == 8.0
        assert rates["cache_read"] == 0.80
        assert rates["cache_write"] == 10.0
        assert rates["output"] == 30.0

    def test_sol_promo_reverts_on_exact_date(self, monkeypatch):
        """MEDIUM: on 2026-11-21 exactly, the promo is NO LONGER active
        (the gate is ``d < _GPT56_SOL_PROMO_UNTIL``, strict less-than)."""
        monkeypatch.setenv("TOKEN_OPTIMIZER_PRICING_AS_OF", "2026-11-21")
        result = m._apply_gpt56_sol_promo_pricing()
        assert result is False, "Promo should be inactive on 2026-11-21 (strict <)"
        assert m.OPENAI_MODEL_PRICING["gpt-5.6-sol"]["input"] == 5.0
        assert m.OPENAI_MODEL_PRICING["gpt-5.6-sol"]["output"] == 30.0

    def test_sol_promo_active_day_before_revert(self, monkeypatch):
        """MEDIUM: on 2026-11-20, the promo is still active."""
        monkeypatch.setenv("TOKEN_OPTIMIZER_PRICING_AS_OF", "2026-11-20")
        result = m._apply_gpt56_sol_promo_pricing()
        assert result is True, "Promo should be active on 2026-11-20"
        assert m.OPENAI_MODEL_PRICING["gpt-5.6-sol"]["input"] == 4.0
        assert m.OPENAI_MODEL_PRICING["gpt-5.6-sol"]["output"] == 20.0

    def test_sol_promo_uses_dated_mechanism_not_bare_constant(self):
        """MEDIUM: the promo reversion is driven by _GPT56_SOL_PROMO_UNTIL
        (a datetime), not a bare boolean constant."""
        assert isinstance(m._GPT56_SOL_PROMO_UNTIL, datetime)
        assert m._GPT56_SOL_PROMO_UNTIL.tzinfo == timezone.utc
        assert m._GPT56_SOL_PROMO_UNTIL.year == 2026
        assert m._GPT56_SOL_PROMO_UNTIL.month == 11
        assert m._GPT56_SOL_PROMO_UNTIL.day == 21

    def test_sol_promo_is_idempotent(self, monkeypatch):
        """Guard: calling _apply_gpt56_sol_promo_pricing multiple times is safe."""
        monkeypatch.setenv("TOKEN_OPTIMIZER_PRICING_AS_OF", "2026-09-14")
        m._apply_gpt56_sol_promo_pricing()
        m._apply_gpt56_sol_promo_pricing()
        assert m.OPENAI_MODEL_PRICING["gpt-5.6-sol"]["input"] == 4.0

    def test_long_context_threshold_is_272k(self):
        """Guard: the OpenAI long-context input threshold."""
        assert m.OPENAI_LONG_CONTEXT_INPUT_THRESHOLD == 272_000


# ===========================================================================
# 4. codex_log_index: SAME TOTALS AS FULL PARSE + INCOMPLETE FLAG
# ===========================================================================

class TestCodexLogIndex:
    """Does the incremental SQLite index produce the SAME totals as a full
    parse?  Is the 'incomplete' flag honest?"""

    @pytest.fixture
    def indexed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(index, "resolve_snapshot_dir", lambda: tmp_path / "index")
        monkeypatch.setattr(cs, "MAX_PARSE_FILE_BYTES", 100)
        return tmp_path / "session.jsonl"

    def _session_records(self, totals=(100, 200)):
        """Build a multi-turn session with cumulative token usage."""
        records = [
            _meta(SID_A),
            _turn_ctx("gpt-6-astra"),
        ]
        for n in totals:
            usage = dict(input_tokens=n, cached_input_tokens=n // 2,
                         output_tokens=n // 5, reasoning_output_tokens=n // 10)
            records += [
                _agent_msg(),
                _tc(usage_cum=usage, usage_last=usage),
            ]
        return records

    def test_index_totals_match_full_parse(self, indexed):
        """HIGH: the incremental index must produce the same token totals
        as a full (non-indexed) parse."""
        write_session(indexed, self._session_records((100, 200, 300)))

        # Parse via index (file is "large" because MAX_PARSE_FILE_BYTES=100)
        indexed_result = cs.parse_session_jsonl(indexed)

        # Parse via full (bypass index by raising the threshold)
        original = cs.MAX_PARSE_FILE_BYTES
        cs.MAX_PARSE_FILE_BYTES = 100 * 1024 * 1024
        try:
            full_result = cs.parse_session_jsonl(indexed)
        finally:
            cs.MAX_PARSE_FILE_BYTES = original

        assert indexed_result["total_input_tokens"] == full_result["total_input_tokens"]
        assert indexed_result["total_output_tokens"] == full_result["total_output_tokens"]
        assert indexed_result["total_cache_read"] == full_result["total_cache_read"]
        assert indexed_result["model_usage"] == full_result["model_usage"]
        assert indexed_result["message_count"] == full_result["message_count"]

    def test_index_totals_match_for_multi_model_session(self, indexed):
        """HIGH: index totals match full parse when the session switches
        models mid-stream."""
        records = [
            _meta(SID_A),
            _turn_ctx("gpt-6-astra"),
            _agent_msg(),
            _tc(usage_cum={"input_tokens": 100, "cached_input_tokens": 50,
                           "output_tokens": 20, "reasoning_output_tokens": 2},
                usage_last={"input_tokens": 100, "cached_input_tokens": 50,
                            "output_tokens": 20, "reasoning_output_tokens": 2}),
            _turn_ctx("gpt-5.6-sol"),
            _agent_msg(),
            _tc(usage_cum={"input_tokens": 300, "cached_input_tokens": 150,
                           "output_tokens": 60, "reasoning_output_tokens": 6},
                usage_last={"input_tokens": 200, "cached_input_tokens": 100,
                            "output_tokens": 40, "reasoning_output_tokens": 4}),
        ]
        write_session(indexed, records)

        indexed_result = cs.parse_session_jsonl(indexed)
        original = cs.MAX_PARSE_FILE_BYTES
        cs.MAX_PARSE_FILE_BYTES = 100 * 1024 * 1024
        try:
            full_result = cs.parse_session_jsonl(indexed)
        finally:
            cs.MAX_PARSE_FILE_BYTES = original

        assert (indexed_result["model_usage"] == full_result["model_usage"]), (
            f"Model usage mismatch: {indexed_result['model_usage']} vs {full_result['model_usage']}"
        )
        assert indexed_result["total_input_tokens"] == full_result["total_input_tokens"]

    def test_incomplete_flag_false_when_fully_indexed(self, indexed):
        """MEDIUM: incomplete is False when the entire file is indexed."""
        write_session(indexed, self._session_records((100,)))
        result = cs.parse_session_jsonl(indexed)
        assert result["incomplete"] is False
        assert result["scan_mode"] == "indexed_full"

    def test_incomplete_flag_true_when_partially_indexed(self, indexed, monkeypatch):
        """MEDIUM: incomplete is True when the file is only partially indexed
        (offset < file size after a bounded pass).

        Uses PASS_BYTES large enough to capture the first few records (so
        parse_session_jsonl returns a result) but small enough that the
        token_count records at the end are not yet indexed."""
        records = self._session_records((100, 200, 300))
        write_session(indexed, records)
        # The first 3 records (meta + turn_ctx + agent_msg) are ~220 bytes.
        # Set PASS_BYTES to capture those but not the token_count records.
        monkeypatch.setattr(index, "PASS_BYTES", 250)
        result = cs.parse_session_jsonl(indexed)
        # parse_session_jsonl returns None when no token_count and no
        # meaningful messages are indexed.  Force at least one drain pass
        # so some records are stored, then check the index info directly.
        records_iter, info = index.records(indexed)
        # Consume to close the connection
        list(records_iter)
        assert info["incomplete"] is True
        assert info["scan_mode"] == "indexing"

    def test_incomplete_flag_true_when_records_skipped(self, indexed):
        """MEDIUM: incomplete is True when records are skipped (too large or
        unparseable).  The flag is honest about data loss."""
        records = self._session_records((100,))
        # Write the session, then append a raw non-JSON line
        write_session(indexed, records)
        with indexed.open("a") as f:
            f.write("THIS_IS_NOT_JSON\n")
        result = cs.parse_session_jsonl(indexed)
        assert result is not None
        assert result["incomplete"] is True

    def test_index_drains_to_complete_in_bounded_passes(self, indexed, monkeypatch):
        """Guard: a file larger than one pass eventually reaches incomplete=False."""
        monkeypatch.setattr(index, "PASS_BYTES", 200)
        write_session(indexed, self._session_records((100, 200, 300)))
        for _ in range(20):
            result = cs.parse_session_jsonl(indexed)
            if not index.pending(indexed):
                break
        assert result["incomplete"] is False
        assert result["total_input_tokens"] == 300

    def test_index_preserves_request_list_for_per_request_pricing(self, indexed):
        """HIGH: the index must preserve the requests[] list so per-request
        long-context pricing works identically to a full parse."""
        records = [
            _meta(SID_A),
            _turn_ctx("gpt-6-astra"),
            _agent_msg(),
            _tc(usage_cum={"input_tokens": 100_000, "cached_input_tokens": 50_000,
                           "output_tokens": 1_000, "reasoning_output_tokens": 0},
                usage_last={"input_tokens": 100_000, "cached_input_tokens": 50_000,
                            "output_tokens": 1_000, "reasoning_output_tokens": 0}),
            _agent_msg(),
            _tc(usage_cum={"input_tokens": 500_000, "cached_input_tokens": 250_000,
                           "output_tokens": 2_000, "reasoning_output_tokens": 0},
                usage_last={"input_tokens": 400_000, "cached_input_tokens": 200_000,
                            "output_tokens": 1_000, "reasoning_output_tokens": 0}),
        ]
        write_session(indexed, records)

        indexed_result = cs.parse_session_jsonl(indexed)
        original = cs.MAX_PARSE_FILE_BYTES
        cs.MAX_PARSE_FILE_BYTES = 100 * 1024 * 1024
        try:
            full_result = cs.parse_session_jsonl(indexed)
        finally:
            cs.MAX_PARSE_FILE_BYTES = original

        # Both should have requests lists
        idx_breakdown = indexed_result["model_usage_breakdown"]["gpt-6-astra"]
        full_breakdown = full_result["model_usage_breakdown"]["gpt-6-astra"]
        idx_reqs = idx_breakdown.get("requests", [])
        full_reqs = full_breakdown.get("requests", [])
        assert len(idx_reqs) == len(full_reqs) == 2
        # And the cost should match
        idx_cost = m._cost_from_model_breakdown({"gpt-6-astra": idx_breakdown})
        full_cost = m._cost_from_model_breakdown({"gpt-6-astra": full_breakdown})
        assert idx_cost == pytest.approx(full_cost, rel=1e-6), (
            f"Index cost {idx_cost} != full cost {full_cost}"
        )


# ===========================================================================
# 5. resolve_session NEVER SUBSTITUTES ANOTHER TASK'S LOG
# ===========================================================================

class TestResolveSession:
    """resolve_session must never substitute another task's log."""

    @pytest.fixture
    def two_sessions(self, tmp_path, monkeypatch):
        """Create two distinct session files in the session roots."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        # Session A
        path_a = sessions_dir / f"rollout-2026-09-06-{SID_A}.jsonl"
        write_session(path_a, [
            _meta(SID_A),
            _turn_ctx("gpt-6-astra"),
            _agent_msg(),
            _tc(usage_cum={"input_tokens": 100, "cached_input_tokens": 50,
                           "output_tokens": 20, "reasoning_output_tokens": 0},
                usage_last={"input_tokens": 100, "cached_input_tokens": 50,
                            "output_tokens": 20, "reasoning_output_tokens": 0}),
        ])

        # Session B
        path_b = sessions_dir / f"rollout-2026-09-07-{SID_B}.jsonl"
        write_session(path_b, [
            _meta(SID_B),
            _turn_ctx("gpt-5.6-sol"),
            _agent_msg(),
            _tc(usage_cum={"input_tokens": 200, "cached_input_tokens": 100,
                           "output_tokens": 40, "reasoning_output_tokens": 0},
                usage_last={"input_tokens": 200, "cached_input_tokens": 100,
                            "output_tokens": 40, "reasoning_output_tokens": 0}),
        ])

        monkeypatch.setattr(cs, "session_roots", lambda: (sessions_dir,))
        return path_a, path_b

    def test_transcript_path_alone_is_trusted(self, two_sessions):
        """Guard: when only transcript_path is given (no session_id), the
        path is returned as-is."""
        path_a, _ = two_sessions
        result = cs.resolve_session(transcript_path=str(path_a))
        assert result == path_a

    def test_session_id_alone_finds_correct_session(self, two_sessions):
        """Guard: when only session_id is given, the correct session is found."""
        path_a, path_b = two_sessions
        result = cs.resolve_session(session_id=SID_A)
        assert result == path_a
        result = cs.resolve_session(session_id=SID_B)
        assert result == path_b

    def test_matching_transcript_and_session_id_returns_transcript(self, two_sessions):
        """Guard: when both match, the transcript is returned."""
        path_a, _ = two_sessions
        result = cs.resolve_session(transcript_path=str(path_a), session_id=SID_A)
        assert result == path_a

    def test_mismatched_transcript_and_session_id_does_not_substitute(self, two_sessions):
        """HIGH: when transcript_path points to session A but session_id is
        for session B, resolve_session must NOT return session B's transcript.

        The docstring says "Resolve a known task without ever substituting
        another active task."  Returning B's log when the caller pointed at
        A's file is a substitution that could attribute B's costs to A's hook.

        Current behavior: falls through to find_session_jsonl_by_id(SID_B)
        and returns B's path.  This test FAILS to prove the substitution.
        """
        path_a, path_b = two_sessions
        result = cs.resolve_session(transcript_path=str(path_a), session_id=SID_B)

        # The correct behavior: return None or path_a, NOT path_b.
        assert result != path_b, (
            f"resolve_session substituted session B's log ({path_b}) when "
            f"the caller pointed at session A's transcript ({path_a}). "
            f"This is a cross-session substitution."
        )

    def test_nonexistent_transcript_with_session_id_finds_by_id(self, two_sessions):
        """Guard: when transcript_path doesn't exist but session_id is valid,
        find by session_id.  This is NOT a substitution (the path was wrong)."""
        path_a, _ = two_sessions
        result = cs.resolve_session(
            transcript_path="/nonexistent/path.jsonl", session_id=SID_A
        )
        assert result == path_a

    def test_both_none_returns_none(self, two_sessions):
        """Guard: no inputs -> None."""
        assert cs.resolve_session() is None

    def test_short_session_id_rejected(self, two_sessions):
        """Guard: a session_id shorter than 6 chars is rejected by
        _safe_session_id, so resolve_session returns None."""
        assert cs._safe_session_id("abc") == ""
        result = cs.resolve_session(session_id="abc")
        assert result is None
