"""Adversarial tests for the Codex coach-integrity port (W1-W6).

Every test here encodes a guarantee the port claims to provide. FAILING tests
mark real gaps found by attacking the port — they are the fix list, not flakes.
Do not xfail or weaken them to go green.

Gaps probed:
  - Ungated detectors (overpowered, weak_model) emit Claude-model routing
    advice under the Codex runtime when session data contains Claude-tier
    model keys (custom model_provider, or a crafted/mixed session log).
  - cache_instability is gated on `== "codex"` only: every OTHER foreign
    runtime (cursor, antigravity, grok, opencode, copilot) still runs the
    Claude-specific scans and resolves a state dir that defaults to
    ~/.claude/token-optimizer/data — a foreign-runtime write into the
    Claude tree.
  - _quality_curve_for_model falls through to "anthropic-default" for any
    Codex model string that is not gpt-5*/codex*/gemini* (o3, gpt-4.1,
    codex-mini, custom-provider names, whitespace-only env vars), so a Codex
    user's quality estimate is labeled with the Anthropic curve.
  - quick_scan's trends quick_win still computes count x inventory-average
    savings — the same fabricated-bound class W1 removed from the coach.
  - Malformed skills_detail frontmatter_tokens (NaN/inf/string) crashes the
    entire coach with ValueError/OverflowError.
  - TOKEN_OPTIMIZER_CONTEXT_SIZE=0 or a negative value propagates an
    impossible context window and crashes quick_scan on division.
  - quick_scan labels Codex state memory "MEMORY.md" — a Claude filename a
    Codex user does not have.
  - _generate_codex_auto_recommendations still claims >1,536-char skill
    descriptions are "silently cut from the skill listing" — the exact
    behavior W6's port asserts does not exist on Codex.
"""
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import measure  # noqa: E402
import runtime_env  # noqa: E402
from detectors import cache_instability, overpowered, weak_model  # noqa: E402


@pytest.fixture
def forced_runtime(monkeypatch):
    """Force a runtime for BOTH the measure module and the detector modules.

    Detectors import detect_runtime from runtime_env directly, so patching
    measure.detect_runtime alone leaves them reading the real env. Setting
    TOKEN_OPTIMIZER_RUNTIME + clearing the lru_cache covers every reader.
    """
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


def _coach_env(monkeypatch, runtime):
    monkeypatch.setattr(measure, "detect_runtime", lambda: runtime)
    monkeypatch.setattr(measure, "detect_context_window", lambda: (258400, "test"))
    monkeypatch.setattr(measure, "_find_all_jsonl_files", lambda **kw: [])
    monkeypatch.setattr(measure, "_get_compression_coverage", lambda **kw: {})


def _base_components():
    return {
        "skills": {"count": 5, "tokens": 50, "names": [f"s{i}" for i in range(5)]},
        "plugin_skills": {"count": 0, "tokens": 0, "names": []},
        "hooks": {"configured": True, "names": ["Stop"]},
    }


def _quick_scan(monkeypatch, codex_cfg, *, env_model=None):
    monkeypatch.setattr(measure, "detect_runtime", lambda: "codex")
    for var in ("CODEX_MODEL", "OPENAI_MODEL"):
        monkeypatch.delenv(var, raising=False)
    if env_model is not None:
        monkeypatch.setenv("CODEX_MODEL", env_model)
    monkeypatch.setattr(measure, "measure_components", lambda: {})
    monkeypatch.setattr(measure, "detect_context_window", lambda: (258400, "test"))
    monkeypatch.setattr(measure, "_collect_trends_data", lambda **kw: None)
    monkeypatch.setattr(measure, "_auto_snapshot", lambda *a, **kw: None)
    monkeypatch.setattr(measure, "_read_codex_config", lambda: codex_cfg)
    return measure.quick_scan(as_json=True)


_CLAUDE_MODEL_TOKENS = ("Sonnet", "Haiku", "Opus", "sonnet", "haiku")


# --- Surface 1/2: ungated detectors leak Claude model-routing advice ---------


def test_overpowered_silent_or_codex_safe_under_codex(forced_runtime):
    """model_usage keyed by a Claude-tier model (custom provider entry in
    config.toml, or a crafted session log) must not yield 'Sonnet' advice
    for a Codex user. respond_to_bash and cache_instability early-return for
    foreign runtimes; overpowered has no gate at all."""
    forced_runtime("codex")
    session = {
        "model_usage": {"claude-opus-4-6": 100_000},
        "total_output_tokens": 2_000,
        "api_calls": 10,
        "tool_calls": {"Read": 20},
    }
    findings = overpowered.detect_overpowered(session)
    assert findings == [], (
        "overpowered emitted Claude-tier model routing advice under Codex: "
        f"{findings}"
    )


def test_overpowered_finding_reaches_codex_coach_output(forced_runtime, monkeypatch, tmp_path):
    """End-to-end: a Codex-scanned session whose log records a Claude-tier
    model name puts 'Sonnet would save' into the coach's fix text."""
    forced_runtime("codex")
    fake = tmp_path / "poisoned.jsonl"
    fake.write_text("{}\n", encoding="utf-8")
    parsed = {
        "total_input_tokens": 100_000,
        "total_output_tokens": 2_000,
        "api_calls": 10,
        "message_count": 10,
        "model_usage": {"claude-opus-4-6": 100_000},
        "tool_calls": {"Read": 20},
    }
    monkeypatch.setattr(measure, "_find_all_jsonl_files", lambda **kw: [(fake, 0, "proj")])
    monkeypatch.setattr(measure, "_parse_session_jsonl", lambda *a, **kw: dict(parsed))
    monkeypatch.setattr(measure, "parse_session_turns", lambda *a, **kw: [])
    monkeypatch.setattr(measure, "detect_context_window", lambda: (258400, "test"))
    monkeypatch.setattr(measure, "_get_compression_coverage", lambda **kw: {})
    result = measure.generate_coach_data(
        components=_base_components(), trends={"skills": {"never_used": []}})
    blob = json.dumps(result["patterns_bad"])
    for token in _CLAUDE_MODEL_TOKENS:
        assert token not in blob, f"{token!r} leaked into Codex coach: {blob}"


def test_weak_model_silent_or_codex_safe_under_codex(forced_runtime):
    """weak_model's 'Consider Sonnet' advice survives in the findings list;
    only triage's savings floor incidentally keeps it out of the coach. The
    detector itself must not emit Claude-tier advice under Codex."""
    forced_runtime("codex")
    session = {
        "model_usage": {"claude-haiku-4-5": 200_000},
        "total_input_tokens": 200_000,
        "tool_calls": {"Read": 15},
    }
    findings = weak_model.detect_weak_model(session)
    assert findings == [], (
        "weak_model emitted Claude-tier advice under Codex: " f"{findings}"
    )


# --- Surface 1b: cache_instability gate is asymmetric — only codex excluded ---


def test_cache_instability_silent_on_non_claude_runtimes(forced_runtime):
    """The gate is `== "codex"`, so cursor/antigravity still run CLAUDE.md
    prefix, .mcp.json and .claude/processes scans and can emit CLAUDE.md
    advice to users who have no CLAUDE.md-based prompt cache."""
    volatile_md = '@import "./status-page.md"\n' + ("stable line\n" * 40)
    for rt in ("cursor", "antigravity"):
        forced_runtime(rt)
        findings = cache_instability.detect_cache_instability(
            {"claude_md_content": volatile_md})
        claude_flavored = [f for f in findings if "CLAUDE.md" in json.dumps(f)]
        assert not claude_flavored, (
            f"cache_instability emitted CLAUDE.md findings under {rt}: "
            f"{claude_flavored}"
        )


def test_cache_instability_never_resolves_state_dir_off_claude(forced_runtime, monkeypatch, tmp_path):
    """Under a foreign runtime the detector must not reach _state_dir at all:
    its default is ~/.claude/token-optimizer/data, so every non-Claude run
    writes detector state into the Claude tree."""
    forced_runtime("cursor")
    calls = []
    monkeypatch.setattr(cache_instability, "_state_dir",
                        lambda: calls.append(1) or tmp_path)
    cache_instability._MCP_SCAN_CACHE.clear()
    cache_instability.detect_cache_instability({})
    assert not calls, "cache_instability resolved its state dir under cursor"


# --- Surface 3: model resolution must never yield the Anthropic curve --------


@pytest.mark.parametrize("cfg_model", ["gpt-4.1", "o3", "codex-mini-latest",
                                       "claude-opus-4-6", "some-custom-model"])
def test_codex_quality_curve_never_anthropic(monkeypatch, cfg_model):
    """Any config.toml model that is not a gpt-5/codex string currently falls
    through to 'anthropic-default': a Codex user's quality estimate is scored
    and labeled with the Anthropic curve."""
    result = _quick_scan(monkeypatch, {"model": cfg_model})
    assert "anthropic" not in result["quality_curve"], (
        f"config model {cfg_model!r} produced curve {result['quality_curve']!r}"
    )


def test_codex_quality_curve_whitespace_env_model(monkeypatch):
    """CODEX_MODEL='   ' is truthy in the `or` chain, wins over config, then
    matches no known family -> anthropic-default for a Codex user."""
    result = _quick_scan(monkeypatch, {"model": "gpt-5.5-codex"}, env_model="   ")
    assert "anthropic" not in result["quality_curve"]


# --- Surface 4: W1 — savings must be measured, not count x inventory avg -----


def test_quick_win_savings_not_count_times_inventory_avg(forced_runtime, monkeypatch):
    """quick_scan's trends quick_win still computes len(never_used) x
    (inventory_tokens // count). With skewed skills (one heavy, many light)
    that reports ~6,000 while the measured unused set is 300 — the fabricated
    bound W1 removed from the coach path."""
    forced_runtime("claude")
    names = [f"skill-{i}" for i in range(30)]
    components = {
        "skills": {"count": 30, "tokens": 6000, "names": names},
        "skills_detail": {n: {"frontmatter_tokens": 10} for n in names},
    }
    monkeypatch.setattr(measure, "measure_components", lambda: components)
    monkeypatch.setattr(measure, "detect_context_window", lambda: (258400, "test"))
    monkeypatch.setattr(measure, "_collect_trends_data",
                        lambda **kw: {"skills": {"never_used": list(names)}})
    monkeypatch.setattr(measure, "_auto_snapshot", lambda *a, **kw: None)
    result = measure.quick_scan(as_json=True)
    qw = result.get("quick_win")
    if qw:
        assert "6,000" not in qw["detail"], (
            f"quick_win fabricated count x avg savings: {qw['detail']}"
        )


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), "corrupt"])
def test_malformed_frontmatter_tokens_never_crash_coach(forced_runtime, monkeypatch, bad_value):
    """int(frontmatter_tokens) raises on NaN/inf/non-numeric values, taking
    down the whole coach. Malformed entries must degrade to 'unmeasured'."""
    _coach_env(monkeypatch, "codex")
    names = [f"skill-{i}" for i in range(30)]
    detail = {n: {"frontmatter_tokens": 10} for n in names}
    detail["skill-0"]["frontmatter_tokens"] = bad_value
    components = {
        "skills": {"count": 30, "tokens": 6000, "names": names},
        "skills_detail": detail,
        "plugin_skills": {"count": 0, "tokens": 0, "names": []},
        "hooks": {"configured": True, "names": ["Stop"]},
    }
    trends = {"skills": {"never_used": list(names)}}
    result = measure.generate_coach_data(components=components, trends=trends)
    unused = {p["name"]: p for p in result["patterns_bad"]}.get("Many Unused Skills") \
        or {p["name"]: p for p in result["patterns_bad"]}["Unused Skill Overhead"]
    assert "lack per-skill measurement" in unused["savings"]


# --- Surface 6: numeric robustness -------------------------------------------


def test_zero_context_size_env_does_not_crash_quick_scan(forced_runtime, monkeypatch, clean_ctx_cache):
    """TOKEN_OPTIMIZER_CONTEXT_SIZE=0 passes int() and yields ctx_window=0;
    quick_scan then divides overhead / 0 -> ZeroDivisionError."""
    forced_runtime("codex")
    monkeypatch.setenv("TOKEN_OPTIMIZER_CONTEXT_SIZE", "0")
    monkeypatch.setattr(measure, "measure_components", lambda: {})
    monkeypatch.setattr(measure, "_collect_trends_data", lambda **kw: None)
    monkeypatch.setattr(measure, "_auto_snapshot", lambda *a, **kw: None)
    monkeypatch.setattr(measure, "_read_codex_config", lambda: {})
    monkeypatch.setattr(measure.codex_session, "find_all_jsonl_files",
                        lambda *a, **kw: [], raising=False)
    result = measure.quick_scan(as_json=True)
    assert result["context_window"] > 0


# --- Surface 1c: residual Claude labels in Codex quick_scan ------------------


def test_codex_offenders_do_not_label_state_memory_as_memory_md(forced_runtime, monkeypatch):
    """Under Codex, memory_md holds Codex state_*.sqlite memory — quick_scan
    still prints the offender as 'MEMORY.md', a file Codex users don't have."""
    forced_runtime("codex")
    components = {"memory_md": {"exists": True, "tokens": 9000, "lines": 0}}
    monkeypatch.setattr(measure, "measure_components", lambda: components)
    monkeypatch.setattr(measure, "detect_context_window", lambda: (258400, "test"))
    monkeypatch.setattr(measure, "_collect_trends_data", lambda **kw: None)
    monkeypatch.setattr(measure, "_auto_snapshot", lambda *a, **kw: None)
    monkeypatch.setattr(measure, "_read_codex_config", lambda: {})
    monkeypatch.setattr(measure.codex_session, "find_all_jsonl_files",
                        lambda *a, **kw: [], raising=False)
    result = measure.quick_scan(as_json=True)
    text = json.dumps(result.get("top_offenders", []))
    assert "MEMORY.md" not in text, f"Claude filename shown to Codex user: {text}"


def test_codex_recommendations_do_not_claim_silent_truncation(forced_runtime):
    """W6's port says Codex does not cut skill descriptions at 1,536 chars,
    yet the Codex auto-recommendations still tell the user the overflow 'is
    silently cut from the skill listing'. One of the two outputs is wrong."""
    forced_runtime("codex")
    components = {
        "skill_frontmatter_quality": {"verbose_skills": [
            {"name": "mega-skill", "description_chars": 2000, "truncated": True},
        ]},
        "hooks": {"configured": True,
                  "names": ["Stop", "UserPromptSubmit", "PostToolUse"]},
    }
    plan, _n = measure._generate_codex_auto_recommendations(
        components, trends={}, days=30)
    assert "truncat" not in plan.lower() and "silently cut" not in plan, (
        f"Codex recommendations contradict the W6 invariant: {plan}"
    )
