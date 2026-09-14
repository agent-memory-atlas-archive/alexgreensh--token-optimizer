"""Adversarial robustness tests for the coach codex-integrity port.

Attack dimension: ROBUSTNESS / MALFORMED-INPUT / CRASH / RESOURCE-EXHAUSTION.

The prior SWE-2 pass fixed frontmatter_tokens NaN/inf and CONTEXT_SIZE=0
divide-by-zero. These tests go further: malformed/truncated JSONL transcripts,
corrupt config.toml, non-dict JSON records, Infinity/NaN in usage fields,
resource exhaustion from unbounded file reads, and NaN propagation through
savings/duration math.

Every test here encodes a graceful-degradation guarantee the coach claims to
provide. FAILING tests mark real gaps found by attacking the port. Do not
xfail or weaken them to go green.

Gaps probed:
  - _parse_session_jsonl (Claude path) crashes on Infinity/NaN in usage
    input_tokens/output_tokens (OverflowError/ValueError at int(), not caught
    by except (PermissionError, OSError)). The Codex adapter uses _safe_int()
    which catches OverflowError; the Claude path does not.
  - _parse_session_jsonl (Claude path) crashes on a valid-JSON-but-non-dict
    record (e.g. `42`, `"hello"`, `true`, `null`) with AttributeError at
    record.get(). The Codex adapter guards with isinstance(record, dict); the
    Claude path does not.
  - generate_coach_data crashes when a JSONL record has Infinity in
    cache_read_input_tokens: the value bypasses int() in the parser, propagates
    as inf through total_cache_read, makes cache_hit_rate = inf/inf = nan, then
    int(total_input * nan) = int(nan) -> ValueError in generate_coach_data's
    unguarded subagent-cost loop.
  - _codex_config_int crashes on model_context_window = inf in config.toml:
    int(inf) -> OverflowError, not caught by except (TypeError, ValueError).
    (nan IS caught because int(nan) -> ValueError, but inf is not.)
  - parse_session_turns (Claude path) crashes on non-dict JSON records and on
    Infinity in usage fields (via _get_model_cost's int()).
  - _extract_costly_prompts crashes on non-dict JSON records (AttributeError
    at record.get(), not caught by except (OSError, PermissionError)).
  - Claude path _parse_session_jsonl has no file-size or line-length guard,
    unlike the Codex adapter (MAX_PARSE_FILE_BYTES / MAX_JSONL_LINE_CHARS).
    A 500MB transcript or a single huge line causes resource exhaustion.
"""
import json
import os
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import measure  # noqa: E402
import runtime_env  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def forced_runtime(monkeypatch):
    """Force a runtime for BOTH the measure module and the detector modules."""
    def _set(name):
        monkeypatch.setenv("TOKEN_OPTIMIZER_RUNTIME", name)
        runtime_env.detect_runtime.cache_clear()
        monkeypatch.setattr(measure, "detect_runtime", lambda: name)
    yield _set
    runtime_env.detect_runtime.cache_clear()


@pytest.fixture
def clean_ctx_cache(monkeypatch):
    monkeypatch.setattr(measure, "_context_window_cache", None)
    yield
    measure._context_window_cache = None


def _claude_jsonl(tmp_path, lines):
    """Write a JSONL file and return its path. Lines can be raw strings."""
    f = tmp_path / "hostile.jsonl"
    f.write_text("".join(l + "\n" for l in lines), encoding="utf-8")
    return f


def _assistant_record(usage_overrides=None):
    """A minimal valid Claude Code assistant record."""
    usage = {"input_tokens": 1000, "output_tokens": 500}
    if usage_overrides:
        usage.update(usage_overrides)
    return json.dumps({
        "type": "assistant",
        "message": {
            "model": "claude-sonnet-4-20250514",
            "usage": usage,
        },
        "timestamp": "2026-01-01T00:00:00Z",
    })


_USER_RECORD = json.dumps({
    "type": "user",
    "message": {"content": "hello world"},
    "timestamp": "2026-01-01T00:00:00Z",
})


# ---------------------------------------------------------------------------
# CRITICAL: Infinity/NaN in JSONL usage fields crashes _parse_session_jsonl
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_value,label", [
    (float("inf"), "Infinity"),
    (float("-inf"), "-Infinity"),
    (float("nan"), "NaN"),
])
def test_parse_session_jsonl_infinity_in_input_tokens(forced_runtime, tmp_path, bad_value, label):
    """Python's json.loads parses Infinity/NaN by default (non-standard JSON
    extension). A hostile or corrupt JSONL transcript with input_tokens=Infinity
    reaches int(inp_tok) at the reported_input line, which raises
    OverflowError (inf) or ValueError (nan). Neither is caught by the
    except (PermissionError, OSError) guard. The Codex adapter uses _safe_int()
    which catches OverflowError; the Claude path does not. The coach must
    degrade to None (skip the file), not crash."""
    forced_runtime("claude")
    # json.dumps emits Infinity/NaN as bare tokens, which json.loads accepts.
    record = {
        "type": "assistant",
        "message": {
            "model": "claude-sonnet-4-20250514",
            "usage": {"input_tokens": bad_value, "output_tokens": 100},
        },
        "timestamp": "2026-01-01T00:00:00Z",
    }
    f = _claude_jsonl(tmp_path, [_USER_RECORD, json.dumps(record)])
    result = measure._parse_session_jsonl(str(f))
    assert result is None or isinstance(result, dict), (
        f"_parse_session_jsonl crashed on {label} in input_tokens instead of degrading"
    )


@pytest.mark.parametrize("bad_value,label", [
    (float("inf"), "Infinity"),
    (float("nan"), "NaN"),
])
def test_parse_session_jsonl_infinity_in_output_tokens(forced_runtime, tmp_path, bad_value, label):
    """Same crash vector as input_tokens, but through output_tokens: the
    reported_output += int(out_tok) line raises before the file is fully
    parsed."""
    forced_runtime("claude")
    record = {
        "type": "assistant",
        "message": {
            "model": "claude-sonnet-4-20250514",
            "usage": {"input_tokens": 100, "output_tokens": bad_value},
        },
        "timestamp": "2026-01-01T00:00:00Z",
    }
    f = _claude_jsonl(tmp_path, [_USER_RECORD, json.dumps(record)])
    result = measure._parse_session_jsonl(str(f))
    assert result is None or isinstance(result, dict), (
        f"_parse_session_jsonl crashed on {label} in output_tokens instead of degrading"
    )


# ---------------------------------------------------------------------------
# CRITICAL: Non-dict JSON record crashes _parse_session_jsonl
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_line", [
    "42",
    '"hello"',
    "true",
    "null",
    "[1, 2, 3]",
])
def test_parse_session_jsonl_non_dict_record(forced_runtime, tmp_path, bad_line):
    """A JSONL line that is valid JSON but not a dict (an int, string, bool,
    null, or array) passes json.loads but then crashes at record.get(...) with
    AttributeError. The Codex adapter guards with isinstance(record, dict);
    the Claude path does not. A truncated or corrupt transcript can easily
    contain such a line."""
    forced_runtime("claude")
    f = _claude_jsonl(tmp_path, [bad_line, _USER_RECORD, _assistant_record()])
    result = measure._parse_session_jsonl(str(f))
    assert result is None or isinstance(result, dict), (
        f"_parse_session_jsonl crashed on non-dict JSON line {bad_line!r}"
    )


# ---------------------------------------------------------------------------
# CRITICAL: Infinity in cache_read_input_tokens crashes generate_coach_data
# ---------------------------------------------------------------------------


def test_infinity_cache_read_crashes_generate_coach_data(forced_runtime, monkeypatch, tmp_path):
    """cache_read_input_tokens=Infinity bypasses int() in the parser (only
    inp_tok/out_tok go through int()). It propagates as inf through
    total_cache_read, making total_full_input=inf and cache_hit_rate=inf/inf=nan.
    In generate_coach_data's unguarded subagent-cost loop:
      cache_read = int(total_input * chr_val)  # int(inf * nan) = int(nan) -> ValueError
    This crashes the entire coach. The coach must skip the corrupt session."""
    forced_runtime("claude")
    record = {
        "type": "assistant",
        "message": {
            "model": "claude-sonnet-4-20250514",
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 100,
                "cache_read_input_tokens": float("inf"),
            },
        },
        "timestamp": "2026-01-01T00:00:00Z",
    }
    f = _claude_jsonl(tmp_path, [_USER_RECORD, json.dumps(record)])

    monkeypatch.setattr(measure, "measure_components", lambda: {
        "skills": {"count": 0, "tokens": 0, "names": []},
        "hooks": {"configured": False, "names": []},
    })
    monkeypatch.setattr(measure, "detect_context_window", lambda: (200000, "test"))
    monkeypatch.setattr(measure, "_collect_trends_data", lambda **kw: None)
    monkeypatch.setattr(measure, "_auto_snapshot", lambda *a, **kw: None)
    monkeypatch.setattr(measure, "_get_compression_coverage", lambda **kw: {})
    monkeypatch.setattr(measure, "_find_all_jsonl_files", lambda days=30: [(f, 0, "proj")])
    # generate_coach_data calls parse_session_turns; make it safe so the
    # crash is isolated to the _parse_session_jsonl -> int(nan) path.
    monkeypatch.setattr(measure, "parse_session_turns", lambda *a, **kw: [])

    result = measure.generate_coach_data()
    assert isinstance(result, dict), (
        "generate_coach_data crashed on Infinity in cache_read_input_tokens "
        "instead of degrading"
    )


# ---------------------------------------------------------------------------
# CRITICAL: config.toml model_context_window = inf crashes _codex_config_int
# ---------------------------------------------------------------------------


def test_codex_config_inf_context_window_crashes(forced_runtime, monkeypatch, tmp_path):
    """TOML 1.0 supports `inf` and `nan` literals. A config.toml with
    `model_context_window = inf` is parsed by tomllib as float('inf'), then
    _codex_config_int does int(inf) -> OverflowError, which is NOT caught by
    except (TypeError, ValueError). (nan IS caught because int(nan) ->
    ValueError, but inf is not.) This crashes detect_context_window and every
    downstream caller (quick_scan, generate_coach_data)."""
    forced_runtime("codex")
    cfg = tmp_path / "config.toml"
    cfg.write_text("model_context_window = inf\n", encoding="utf-8")
    monkeypatch.setattr(measure, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(measure, "_codex_config_cache", None)
    monkeypatch.setattr(measure, "_context_window_cache", None)
    monkeypatch.setattr(measure, "_latest_codex_logged_context_window",
                        lambda: (None, None))
    # Should return None (skip the corrupt value), not crash.
    val = measure._codex_config_int("model_context_window")
    assert val is None, (
        f"_codex_config_int should return None for inf, got {val!r} or crashed"
    )


def test_codex_config_inf_context_window_crashes_detect_context_window(
        forced_runtime, monkeypatch, tmp_path, clean_ctx_cache):
    """End-to-end: detect_context_window calls _codex_config_int when no logged
    window is found. A corrupt config.toml with inf crashes the whole chain."""
    forced_runtime("codex")
    cfg = tmp_path / "config.toml"
    cfg.write_text("model_context_window = inf\n", encoding="utf-8")
    monkeypatch.setattr(measure, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(measure, "_codex_config_cache", None)
    monkeypatch.setattr(measure, "_latest_codex_logged_context_window",
                        lambda: (None, None))
    ctx_window, _source = measure.detect_context_window()
    assert ctx_window > 0, (
        "detect_context_window crashed on inf in config.toml instead of "
        "falling back to the default"
    )


# ---------------------------------------------------------------------------
# CRITICAL: parse_session_turns (Claude path) crashes on non-dict and Infinity
# ---------------------------------------------------------------------------


def test_parse_session_turns_non_dict_record(forced_runtime, tmp_path):
    """parse_session_turns (Claude path) has the same non-dict crash as
    _parse_session_jsonl: record.get('type') -> AttributeError when record is
    not a dict. Not caught by except (PermissionError, OSError)."""
    forced_runtime("claude")
    f = _claude_jsonl(tmp_path, ["42", _USER_RECORD, _assistant_record()])
    result = measure.parse_session_turns(str(f))
    assert isinstance(result, list), (
        "parse_session_turns crashed on non-dict JSON line instead of returning []"
    )


def test_parse_session_turns_infinity_in_usage(forced_runtime, tmp_path):
    """parse_session_turns passes raw usage values (no int() guard) to
    _get_model_cost, which does int(input_tokens or 0) -> OverflowError when
    input_tokens is Infinity. Not caught by except (PermissionError, OSError)."""
    forced_runtime("claude")
    record = {
        "type": "assistant",
        "message": {
            "model": "claude-sonnet-4-20250514",
            "usage": {"input_tokens": float("inf"), "output_tokens": 100},
        },
        "timestamp": "2026-01-01T00:00:00Z",
    }
    f = _claude_jsonl(tmp_path, [_USER_RECORD, json.dumps(record)])
    result = measure.parse_session_turns(str(f))
    assert isinstance(result, list), (
        "parse_session_turns crashed on Infinity in usage instead of returning []"
    )


# ---------------------------------------------------------------------------
# CRITICAL: _extract_costly_prompts crashes on non-dict JSON record
# ---------------------------------------------------------------------------


def test_extract_costly_prompts_non_dict_record(forced_runtime, tmp_path):
    """_extract_costly_prompts does record.get('type') on every parsed line.
    A non-dict JSON record (e.g. `42`) crashes with AttributeError, not caught
    by except (OSError, PermissionError)."""
    forced_runtime("claude")
    f = _claude_jsonl(tmp_path, [
        "42",
        json.dumps({"type": "user", "message": {"content": "hello world test"}}),
        _assistant_record(),
    ])
    result = measure._extract_costly_prompts(str(f))
    assert isinstance(result, list), (
        "_extract_costly_prompts crashed on non-dict JSON line instead of returning []"
    )


# ---------------------------------------------------------------------------
# HIGH: Claude path has no file-size / line-length guard (resource exhaustion)
# ---------------------------------------------------------------------------


def test_parse_session_jsonl_no_line_length_guard(forced_runtime, tmp_path):
    """The Codex adapter (_iter_json_records) skips lines > 8MB
    (MAX_JSONL_LINE_CHARS = 8 * 1024 * 1024). The Claude path
    (_parse_session_jsonl in measure.py) has NO line-length guard: it reads
    and json.loads every line regardless of size. A single 9MB line (above the
    Codex threshold) is processed unbounded, demonstrating the asymmetry. The
    coach must bound its work like the Codex adapter does."""
    forced_runtime("claude")
    # Write a single line that exceeds the Codex adapter's MAX_JSONL_LINE_CHARS.
    huge_content = "x" * (9 * 1024 * 1024)  # 9MB string > 8MB Codex limit
    record = json.dumps({
        "type": "user",
        "message": {"content": huge_content},
        "timestamp": "2026-01-01T00:00:00Z",
    })
    f = _claude_jsonl(tmp_path, [record, _assistant_record()])
    result = measure._parse_session_jsonl(str(f))
    # The Codex adapter would skip the 9MB line and return a result without
    # the huge content. The Claude path has no guard, so it processes the line
    # and the huge content ends up in the topic. Assert the huge line was
    # skipped (topic does not contain the huge content), which fails because
    # the Claude path processes it unbounded.
    # The Codex adapter would skip the 9MB line entirely. If the Claude path
    # had the same guard, the huge user record would be skipped and the topic
    # would be None (no other user record in this file). Instead the Claude
    # path processes the 9MB line, so the topic is a truncated version of the
    # huge content. Assert the line was skipped (topic is None), which fails
    # because the Claude path processes it unbounded.
    topic = (result or {}).get("topic")
    assert topic is None, (
        "Claude path _parse_session_jsonl has no line-length guard: a 9MB "
        f"line (above the Codex adapter's 8MB MAX_JSONL_LINE_CHARS) was "
        f"processed instead of being skipped (topic={str(topic)[:80]!r})"
    )


# ---------------------------------------------------------------------------
# HIGH: Infinity in cache_creation_input_tokens crashes generate_coach_data
# ---------------------------------------------------------------------------


def test_infinity_cache_create_crashes_generate_coach_data(forced_runtime, monkeypatch, tmp_path):
    """cache_creation_input_tokens=Infinity bypasses int() in the parser,
    propagates as inf through total_cache_create, making total_full_input=inf.
    If cache_hit_rate becomes 0.0 (finite cache_read / inf), then
    int(total_input * 0.0) = int(inf * 0.0) = int(nan) -> ValueError in
    generate_coach_data's unguarded subagent-cost loop."""
    forced_runtime("claude")
    record = {
        "type": "assistant",
        "message": {
            "model": "claude-sonnet-4-20250514",
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 100,
                "cache_creation_input_tokens": float("inf"),
            },
        },
        "timestamp": "2026-01-01T00:00:00Z",
    }
    f = _claude_jsonl(tmp_path, [_USER_RECORD, json.dumps(record)])

    monkeypatch.setattr(measure, "measure_components", lambda: {
        "skills": {"count": 0, "tokens": 0, "names": []},
        "hooks": {"configured": False, "names": []},
    })
    monkeypatch.setattr(measure, "detect_context_window", lambda: (200000, "test"))
    monkeypatch.setattr(measure, "_collect_trends_data", lambda **kw: None)
    monkeypatch.setattr(measure, "_auto_snapshot", lambda *a, **kw: None)
    monkeypatch.setattr(measure, "_get_compression_coverage", lambda **kw: {})
    monkeypatch.setattr(measure, "_find_all_jsonl_files", lambda days=30: [(f, 0, "proj")])
    monkeypatch.setattr(measure, "parse_session_turns", lambda *a, **kw: [])

    result = measure.generate_coach_data()
    assert isinstance(result, dict), (
        "generate_coach_data crashed on Infinity in cache_creation_input_tokens "
        "instead of degrading"
    )


# ---------------------------------------------------------------------------
# HIGH: _extract_skills_and_agents_from_subagent crashes on non-dict record
# ---------------------------------------------------------------------------


def test_extract_subagent_skills_non_dict_record(forced_runtime, tmp_path):
    """_extract_skills_and_agents_from_subagent does record.get('type') on
    every parsed line. A non-dict JSON record crashes with AttributeError,
    not caught by except (PermissionError, OSError)."""
    forced_runtime("claude")
    f = _claude_jsonl(tmp_path, [
        "42",
        json.dumps({
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "name": "Skill",
                             "input": {"skill": "my-skill"}}],
            },
        }),
    ])
    skills, subagents = measure._extract_skills_and_agents_from_subagent(str(f))
    assert isinstance(skills, dict) and isinstance(subagents, dict), (
        "_extract_skills_and_agents_from_subagent crashed on non-dict JSON line"
    )
