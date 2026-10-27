"""Reproduction + pin for the reported anomaly: fresh session, host context
bar 16%, zero compactions, ContextQ score 59.

Root cause: ``compute_quality_score`` divided transcript ``context_tokens``
by an INFERRED context window (200k from env/config detection) while the real
session ran on a 1M window. 140k/200k produced a phantom 70% fill (fill_score
~18, ResourceHealth ~59) even though the host's own used_percentage — the same
number the statusline bar renders — said 16%. The fresh live-fill gate (10s)
discards same-session host readings older than 10 seconds, which is most of
them during long tool runs, so the wrong-denominator arithmetic won silently.

Fix contract pinned here: a same-session host fill reading that is still
recent (<5min, matching the statusline staleness convention) is a sanity
anchor. When it disagrees with our computed fill by >10 points, the host
wins — the host measures the real window; our denominator is two layers of
inference. The disagreement is still recorded in the breakdown so a real
misdetection stays diagnosable.
"""
from __future__ import annotations

import importlib
import json
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


@pytest.fixture
def m(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    qc = tmp_path / "qc"
    qc.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "QUALITY_CACHE_DIR", qc, raising=True)
    monkeypatch.setattr(mod, "CLAUDE_DIR", tmp_path, raising=True)
    yield mod, qc, tmp_path
    if "measure" in sys.modules:
        del sys.modules["measure"]


def _qdata(**kw):
    """Minimal fresh-session quality_data at a true ~16% fill on a 1M window:
    ~140-160k of usage, a handful of messages, no compactions, no waste."""
    d = {
        "messages": [(0, "user", 4000, True), (1, "assistant", 8000, True)],
        "tool_results": [(2, "t1", 3000, False)],
        "tool_result_meta": [],
        "system_reminders": [],
        "reads": [],
        "writes": [],
        "compactions": 0,
        "tool_calls": 1,
        "agent_dispatches": [],
        "decisions": [],
        "total_entries": 3,
        "context_tokens": 140_000,
        "model": "claude-sonnet-4-6",
        # Session supplies no window -> the inferred global wins (or loses).
        "model_context_window": None,
        "topic": "fresh session",
    }
    d.update(kw)
    return d


def _write_live_fill(qc: Path, sid: str, pct: float, age_s: float):
    (qc / "live-fill.json").write_text(json.dumps({
        "used_percentage": pct,
        "session_id": sid,
        # statusline writes a JS (ms) timestamp
        "timestamp": int((time.time() - age_s) * 1000),
    }), encoding="utf-8")


def test_wrong_denominator_reproduces_reported_59(m, monkeypatch):
    """THE anomaly: 140k tokens / inferred 200k window = phantom 70% fill ->
    score ~59 while the host bar reads 16%. Post-fix the stale-but-same-session
    host reading wins the sanity check and the score lands near the 16% curve."""
    mod, qc, _ = m
    monkeypatch.setattr(mod, "detect_context_window",
                        lambda: (200_000, "env: CLAUDE_CODE_DISABLE_1M_CONTEXT"))
    sid = "sess-anomaly-1"
    # Same-session host reading 45s old — past the 10s authoritative gate, so
    # pre-fix it was ignored entirely and the phantom 70% filled in.
    _write_live_fill(qc, sid, 16.0, age_s=45)

    result = mod.compute_quality_score(_qdata(), session_id=sid)

    # Pre-fix this was ~59; the corrected score must sit near the 16% curve.
    assert result["score"] >= 80, (
        f"score {result['score']} — phantom-fill bug reproduced, host sanity "
        "did not rescue the wrong denominator")
    cfd = result["breakdown"]["context_fill_degradation"]
    assert cfd["fill_pct"] < 25
    assert cfd["host_disagreement"] is not None


def test_disagreement_is_recorded_even_without_override(m, monkeypatch):
    """A host reading that agrees roughly with our arithmetic changes nothing
    and records nothing."""
    mod, qc, _ = m
    monkeypatch.setattr(mod, "detect_context_window",
                        lambda: (1_000_000, "default (1M)"))
    sid = "sess-agree"
    _write_live_fill(qc, sid, 15.0, age_s=60)
    result = mod.compute_quality_score(_qdata(), session_id=sid)
    # 140k/1M = 14% vs host 15% — within margin, no disagreement, healthy score.
    assert result["breakdown"]["context_fill_degradation"]["host_disagreement"] is None
    assert result["score"] >= 80


def test_stale_host_beyond_sanity_window_does_not_override(m, monkeypatch):
    """A live-fill older than the sanity cap describes a different moment and
    must not drag a genuinely-filled session's score back up."""
    mod, qc, _ = m
    monkeypatch.setattr(mod, "detect_context_window",
                        lambda: (200_000, "env: CLAUDE_CODE_DISABLE_1M_CONTEXT"))
    sid = "sess-stale"
    _write_live_fill(qc, sid, 16.0, age_s=3600)
    result = mod.compute_quality_score(_qdata(), session_id=sid)
    # No rescue: the phantom 70% stands (that is its own bug to flag, but the
    # sanity anchor is correctly out of reach).
    assert result["score"] < 80


def test_other_session_live_fill_never_used(m, monkeypatch):
    """The sanity anchor only applies to THIS session's host reading."""
    mod, qc, _ = m
    monkeypatch.setattr(mod, "detect_context_window",
                        lambda: (200_000, "env: CLAUDE_CODE_DISABLE_1M_CONTEXT"))
    _write_live_fill(qc, "somebody-elses-session", 16.0, age_s=30)
    result = mod.compute_quality_score(_qdata(), session_id="sess-mine")
    assert result["score"] < 80


def test_fresh_host_still_authoritative(m, monkeypatch):
    """The existing <10s path is unchanged: fresh host fill is used directly."""
    mod, qc, _ = m
    monkeypatch.setattr(mod, "detect_context_window",
                        lambda: (200_000, "default"))
    sid = "sess-live"
    _write_live_fill(qc, sid, 16.0, age_s=2)
    result = mod.compute_quality_score(_qdata(), session_id=sid)
    assert result["score"] >= 80
    assert result["breakdown"]["context_fill_degradation"]["fill_pct"] == 16.0


def _write_live_fill_tokens(qc: Path, sid: str, pct: float, age_s: float, tokens: int, window: int):
    (qc / "live-fill.json").write_text(json.dumps({
        "used_percentage": pct,
        "context_tokens": tokens,
        "context_window": window,
        "session_id": sid,
        "timestamp": int((time.time() - age_s) * 1000),
    }), encoding="utf-8")


def test_pre_compact_host_reading_does_not_override_post_compact_transcript(m, monkeypatch):
    """session_id survives /compact, so a 60s-old host reading from
    just before the compact (92%, 184k tokens) still "describes this session".
    The transcript now says 20k tokens: the reading describes a different
    moment (a numerator change, not a wrong denominator), so it must not win."""
    mod, qc, _ = m
    monkeypatch.setattr(mod, "detect_context_window", lambda: (200_000, "default"))
    sid = "sess-post-compact"
    _write_live_fill_tokens(qc, sid, 92.0, age_s=60, tokens=184_000, window=200_000)
    result = mod.compute_quality_score(_qdata(context_tokens=20_000, compactions=1), session_id=sid)
    fd = result["breakdown"]["context_fill_degradation"]
    assert fd.get("fill_source") != "host-stale-override"
    assert fd["fill_pct"] == pytest.approx(10.0, abs=0.5)
    assert result["score"] >= 80


def test_denominator_error_still_overrides_when_host_tokens_match(m, monkeypatch):
    """The override this check exists for is unchanged: the host's token count
    matches ours (same moment), only the window we inferred is wrong."""
    mod, qc, _ = m
    monkeypatch.setattr(mod, "detect_context_window",
                        lambda: (200_000, "env: CLAUDE_CODE_DISABLE_1M_CONTEXT"))
    sid = "sess-denominator"
    _write_live_fill_tokens(qc, sid, 14.0, age_s=45, tokens=145_000, window=1_000_000)
    result = mod.compute_quality_score(_qdata(context_tokens=140_000), session_id=sid)
    fd = result["breakdown"]["context_fill_degradation"]
    assert fd.get("fill_source") == "host-stale-override"
    assert result["score"] >= 80

# Advisor responses sum executor iterations for billing, not context occupancy.
def _advisor_usage():
    def executor(ctx, read):
        return {"type": "message", "input_tokens": 100,
                "cache_creation_input_tokens": ctx - read - 100,
                "cache_read_input_tokens": read, "output_tokens": 12}
    return {"input_tokens": 200, "cache_creation_input_tokens": 180_827,
            "cache_read_input_tokens": 800_000, "output_tokens": 24,
            "iterations": [executor(489_876, 400_000),
                           {"type": "advisor_message", "input_tokens": 494_410},
                           executor(491_151, 400_000)]}


def _advisor_transcript(mod, root, usage):
    project = root / "projects" / "-advisor"
    project.mkdir(parents=True, exist_ok=True)
    path = project / "advisor-session.jsonl"
    path.write_text(json.dumps({"type": "assistant",
        "timestamp": "2026-10-27T10:00:00Z", "requestId": "advisor-request",
        "message": {"model": "claude-sonnet-5-5", "usage": usage,
                    "content": [{"type": "tool_use", "id": "tool-advisor",
                                 "name": "advisor", "input": {}}]}}) + "\n",
        encoding="utf-8")
    return path


def test_advisor_context_uses_final_executor_without_changing_billing(m, monkeypatch):
    mod, _, root = m
    usage = _advisor_usage()
    path = _advisor_transcript(mod, root, usage)
    q = mod._parse_jsonl_for_quality(path)
    assert q["context_tokens"] == 491_151  # not the 981,027 billed sum
    q["model_context_window"] = 1_000_000
    monkeypatch.setattr(mod, "detect_context_window", lambda: (1_000_000, "test"))
    score = mod.compute_quality_score(q)
    assert score["breakdown"]["context_fill_degradation"]["fill_pct"] == pytest.approx(49.1151, abs=.1)
    assert mod._transcript_last_turn("advisor-session")[0] == 491_151
    replay = mod._advice_session_data(path)
    assert replay["turns"] == [491_151]
    assert replay["cache_reads"] == [800_000]
    assert mod._keepwarm_prefix_proxy(usage) == 491_051
    # These are billing APIs: every paid executor iteration still counts.
    billed_session = mod._parse_session_jsonl(path)
    billed_turn = mod.parse_session_turns(path)[0]
    assert billed_turn["input_tokens"] == 200
    assert billed_turn["cache_read"] == 800_000
    assert billed_turn["cache_creation"] == 180_827
    assert billed_turn["output_tokens"] == 24
    plain = dict(usage)
    plain.pop("iterations")
    baseline = _advisor_transcript(mod, root, plain)
    assert mod.parse_session_turns(baseline)[0]["cost_usd"] == billed_turn["cost_usd"]
    assert mod._parse_session_jsonl(baseline) == billed_session


@pytest.mark.parametrize("iterations", [None, "x", {}, [], [None, 5],
    [{"type": "advisor_message", "input_tokens": 600_000}],
    [{"type": "message"}],
    [{"type": "message", "input_tokens": 50}, {"type": "message"}],
    [{"type": "message", "input_tokens": "bad"}],
    [{"type": "message", "input_tokens": float("inf")}],
])
def test_unusable_executor_usage_keeps_aggregate_fallback(m, iterations):
    mod, _, root = m
    usage = {"input_tokens": 100, "cache_read_input_tokens": 200,
             "cache_creation_input_tokens": 30, "iterations": iterations}
    path = _advisor_transcript(mod, root, usage)
    assert mod._parse_jsonl_for_quality(path)["context_tokens"] == 330
    assert mod._advice_session_data(path)["turns"] == [330]
    assert mod._transcript_last_turn("advisor-session")[0] == 330
    assert mod._keepwarm_prefix_proxy(usage) == 230


def test_advisor_tail_is_not_executor_and_nested_cache_is_not_lost(m):
    mod, _, root = m
    usage = _advisor_usage()
    last = usage["iterations"][-1]
    last.pop("cache_creation_input_tokens")
    last["cache_creation"] = {"ephemeral_1h_input_tokens": 50_000,
                              "ephemeral_5m_input_tokens": 41_051}
    usage["iterations"] += [None, {"type": "advisor_message", "input_tokens": 900_000}]
    path = _advisor_transcript(mod, root, usage)
    assert mod._parse_jsonl_for_quality(path)["context_tokens"] == 491_151
    assert mod._advice_session_data(path)["turns"] == [491_151]
    assert mod._keepwarm_prefix_proxy(usage) == 491_051


@pytest.mark.parametrize("usage, expected", [
    ({"input_tokens": 10, "cache_creation": {
        "ephemeral_1h_input_tokens": "20", "ephemeral_5m_input_tokens": 30}}, 60),
    ({"input_tokens": 10, "cache_creation_input_tokens": 40,
      "cache_creation": {"ephemeral_1h_input_tokens": 40}}, 50),
    ({"input_tokens": "bad", "cache_read_input_tokens": float("nan")}, 0),
    (None, 0), ([], 0),
])
def test_context_token_count_handles_cache_shapes(m, usage, expected):
    mod, _, _ = m
    assert mod._context_token_count(usage) == expected


def test_second_reported_advisor_record_uses_latest_executor(m):
    mod, _, root = m
    usage = {"input_tokens": 1_032_706, "iterations": [
        {"type": "message", "input_tokens": 515_846},
        {"type": "advisor_message", "input_tokens": 900_000},
        {"type": "message", "input_tokens": 516_860}]}
    path = _advisor_transcript(mod, root, usage)
    assert mod._parse_jsonl_for_quality(path)["context_tokens"] == 516_860
    assert mod._advice_session_data(path)["turns"] == [516_860]
    assert mod._transcript_last_turn("advisor-session")[0] == 516_860


@pytest.mark.parametrize("selected", [False, True])
def test_top_level_ttl_splits_describe_plain_and_executor_context(m, selected):
    mod, _, root = m
    executor = {"type": "message", "input_tokens": 100,
                "cache_read_input_tokens": 400_000,
                "ephemeral_1h_input_tokens": 50_000,
                "ephemeral_5m_input_tokens": 41_051}
    usage = ({"input_tokens": 200, "cache_read_input_tokens": 800_000,
              "iterations": [executor]} if selected else executor)
    path = _advisor_transcript(mod, root, usage)
    assert mod._parse_jsonl_for_quality(path)["context_tokens"] == 491_151
    assert mod._advice_session_data(path)["turns"] == [491_151]
    assert mod._transcript_last_turn("advisor-session")[0] == 491_151
    assert mod._keepwarm_prefix_proxy(usage) == 491_051
