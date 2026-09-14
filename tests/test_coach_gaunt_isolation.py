"""Adversarial tests for RUNTIME ISOLATION / CROSS-RUNTIME LEAKAGE.

Attack dimension: find paths where Claude-only content (CLAUDE.md, model names
Sonnet/Haiku/Opus, cache-instability findings, Claude cache guidance) reaches a
Codex or other foreign runtime, OR where a foreign runtime reads Claude's
data/settings.

The prior SWE-2 pass (commit da696cff) fixed 17 gaps but only special-cased
**codex**.  Every guard uses ``detect_runtime() == "codex"`` or
``is_codex = detect_runtime() == "codex"``, leaving the five other foreign
runtimes — cursor, antigravity, grok, opencode, copilot — with Claude-specific
content.  These tests probe those non-codex foreign runtimes and deeper
isolation surfaces the prior pass missed.

Every test here is a FAILING test marking a real gap.  Do not xfail or weaken
them to go green.
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
from detectors import output_waste  # noqa: E402

# Runtimes the prior pass fixed: codex only.
# Runtimes it MISSED: these five.
_FOREIGN_RUNTIMES = ["cursor", "antigravity", "grok", "opencode", "copilot"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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


def _base_components():
    return {
        "skills": {"count": 5, "tokens": 50, "names": [f"s{i}" for i in range(5)]},
        "plugin_skills": {"count": 0, "tokens": 0, "names": []},
        "hooks": {"configured": True, "names": ["Stop"]},
    }


def _coach_env(monkeypatch, runtime):
    monkeypatch.setattr(measure, "detect_runtime", lambda: runtime)
    monkeypatch.setattr(measure, "detect_context_window", lambda: (258400, "test"))
    monkeypatch.setattr(measure, "_find_all_jsonl_files", lambda **kw: [])
    monkeypatch.setattr(measure, "_get_compression_coverage", lambda **kw: {})


# ===========================================================================
# HIGH 1: generate_coach_data leaks "CLAUDE.md" / "MEMORY.md" labels to
#         ALL non-codex foreign runtimes (not just codex)
# ===========================================================================

@pytest.mark.parametrize("rt", _FOREIGN_RUNTIMES)
def test_coach_data_no_claude_md_label_for_foreign_runtimes(forced_runtime, monkeypatch, rt):
    """generate_coach_data uses ``is_codex = detect_runtime() == "codex"``
    and then ``instruction_label = "AGENTS.md" if is_codex else "CLAUDE.md"``.

    The prior pass only special-cased codex.  For cursor / antigravity / grok /
    opencode / copilot the coach output still labels the instruction-file
    pattern "CLAUDE.md Could Be Leaner" and tells the user to edit CLAUDE.md —
    a file none of these runtimes use.
    """
    forced_runtime(rt)
    # Provide a claude_md component large enough to trigger the pattern.
    components = _base_components()
    components["claude_md_global"] = {
        "exists": True,
        "tokens": 8000,
        "lines": 250,
        "path": "/fake/CLAUDE.md",
    }
    monkeypatch.setattr(measure, "detect_context_window", lambda: (200000, "test"))
    monkeypatch.setattr(measure, "_find_all_jsonl_files", lambda **kw: [])
    monkeypatch.setattr(measure, "_get_compression_coverage", lambda **kw: {})
    monkeypatch.setattr(measure, "_collect_trends_data", lambda **kw: None)
    result = measure.generate_coach_data(components=components, trends={})
    blob = json.dumps(result.get("patterns_bad", []))
    assert "CLAUDE.md" not in blob, (
        f"Coach output for {rt} contains 'CLAUDE.md' label — "
        f"Claude-only content leaked to a foreign runtime: {blob}"
    )


@pytest.mark.parametrize("rt", _FOREIGN_RUNTIMES)
def test_coach_data_no_memory_md_label_for_foreign_runtimes(forced_runtime, monkeypatch, rt):
    """Same is_codex gate: memory_label = "Codex memories" if is_codex else
    "MEMORY.md".  For non-codex foreign runtimes the coach labels the memory
    component "MEMORY.md" — a Claude filename these runtimes don't have.
    """
    forced_runtime(rt)
    components = _base_components()
    components["memory_md"] = {"exists": True, "tokens": 9000, "lines": 250}
    monkeypatch.setattr(measure, "detect_context_window", lambda: (200000, "test"))
    monkeypatch.setattr(measure, "_find_all_jsonl_files", lambda **kw: [])
    monkeypatch.setattr(measure, "_get_compression_coverage", lambda **kw: {})
    monkeypatch.setattr(measure, "_collect_trends_data", lambda **kw: None)
    result = measure.generate_coach_data(components=components, trends={})
    blob = json.dumps(result.get("patterns_bad", []))
    assert "MEMORY.md" not in blob, (
        f"Coach output for {rt} contains 'MEMORY.md' label — "
        f"Claude-only filename leaked to a foreign runtime: {blob}"
    )


# ===========================================================================
# HIGH 2: generate_auto_recommendations generates Claude-specific advice
#         (CLAUDE.md, MEMORY.md, Anthropic, ~/.claude paths) for ALL
#         non-codex foreign runtimes
# ===========================================================================

@pytest.mark.parametrize("rt", _FOREIGN_RUNTIMES)
def test_auto_recommendations_no_claude_content_for_foreign_runtimes(forced_runtime, monkeypatch, rt):
    """generate_auto_recommendations dispatches to
    _generate_codex_auto_recommendations ONLY for codex.  Every other foreign
    runtime falls through to the Claude path, which emits:

      - "Trim MEMORY.md from …" / "Claude auto-loads the first 200 lines …"
      - "Slim <path> (… tokens, … lines; target under 200 lines)" with
        "Anthropic recommends under 200 lines per CLAUDE.md file"
      - "~/.claude/CLAUDE.md ~/.claude/rules/ ~/.claude/skills/" grep advice

    The code even acknowledges this in a comment (line ~8117):
    "Codex is dispatched to _generate_codex_auto_recommendations and never
    reaches here, so this only shapes Claude / other-foreign-runtime advice."
    """
    forced_runtime(rt)
    components = _base_components()
    # Trigger the MEMORY.md rule.
    components["memory_md"] = {"exists": True, "tokens": 9000, "lines": 250}
    # Trigger the CLAUDE.md rule.
    components["claude_md_global"] = {
        "exists": True, "tokens": 8000, "lines": 250, "path": "/fake/CLAUDE.md",
    }
    monkeypatch.setattr(measure, "detect_context_window", lambda: (200000, "test"))
    plan, _n = measure.generate_auto_recommendations(
        components=components, trends=None, days=30)
    plan_lower = plan.lower()
    # Claude-specific tokens that should never appear for a foreign runtime.
    for token in ("memory.md", "claude auto-loads", "anthropic recommends",
                  "~/.claude/"):
        assert token not in plan_lower, (
            f"Auto-recommendations for {rt} contain Claude-specific content "
            f"'{token}': {plan[:500]}"
        )


# ===========================================================================
# HIGH 3: output_waste detector leaks "CLAUDE.md" in suggestions to ALL
#         non-codex foreign runtimes
# ===========================================================================

@pytest.mark.parametrize("rt", _FOREIGN_RUNTIMES)
def test_output_waste_no_claude_md_for_foreign_runtimes(forced_runtime, rt):
    """output_waste.py line 47:
    ``_instr = "AGENTS.md" if detect_runtime() == "codex" else "CLAUDE.md"``

    Only codex is special-cased.  For cursor / antigravity / grok / opencode /
    copilot the suggestion tells the user to "Add 'Be concise' … to CLAUDE.md"
    — a file these runtimes don't use.
    """
    forced_runtime(rt)
    turns = []
    for i in range(6):
        turns.append({
            "tools_used": ["Read"],
            "output_tokens": 3000,
            "input_tokens": 500,
            "assistant_text": "x" * 300,
        })
    session_data = {
        "turns": turns,
        "total_output_tokens": 18000,
    }
    findings = output_waste.detect_output_waste(session_data)
    blob = json.dumps(findings)
    assert "CLAUDE.md" not in blob, (
        f"output_waste detector for {rt} suggests editing CLAUDE.md — "
        f"Claude-only content leaked to a foreign runtime: {blob}"
    )


# ===========================================================================
# HIGH 4: quality_curve resolves to "anthropic-default" for ALL non-codex
#         foreign runtimes (the fallback to a non-anthropic curve only
#         fires for codex)
# ===========================================================================

@pytest.mark.parametrize("rt", _FOREIGN_RUNTIMES)
def test_quality_curve_no_anthropic_default_for_foreign_runtimes(forced_runtime, monkeypatch, rt):
    """quick_scan resolves the quality curve model only for codex:

        if _rt == "codex":
            _qmodel = CODEX_MODEL or OPENAI_MODEL or _codex_config_model() or "codex"
        else:
            _qmodel = CLAUDE_MODEL or ANTHROPIC_MODEL

    For non-codex foreign runtimes _qmodel is resolved from Claude-specific env
    vars.  When neither is set _qmodel is None, and _quality_curve_for_model(None)
    returns "anthropic-default".  The fallback to a non-anthropic curve
    (``if _rt == "codex" and _qcurve == "anthropic-default"``) only fires for
    codex, so every other foreign runtime gets its quality estimate labeled
    with the Anthropic curve.
    """
    forced_runtime(rt)
    # Clear any Claude model env vars so _qmodel falls through to None.
    for var in ("CLAUDE_MODEL", "ANTHROPIC_MODEL", "CODEX_MODEL", "OPENAI_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(measure, "measure_components", lambda: {})
    monkeypatch.setattr(measure, "detect_context_window", lambda: (258400, "test"))
    monkeypatch.setattr(measure, "_collect_trends_data", lambda **kw: None)
    monkeypatch.setattr(measure, "_auto_snapshot", lambda *a, **kw: None)
    monkeypatch.setattr(measure, "_read_codex_config", lambda: {})
    monkeypatch.setattr(measure.codex_session, "find_all_jsonl_files",
                        lambda *a, **kw: [], raising=False)
    result = measure.quick_scan(as_json=True)
    assert "anthropic" not in result.get("quality_curve", ""), (
        f"Quality curve for {rt} is '{result.get('quality_curve')}' — "
        f"Claude/Anthropic curve label leaked to a foreign runtime"
    )


# ===========================================================================
# MEDIUM 5: detect_context_window reads CLAUDE_MODEL / ANTHROPIC_MODEL for
#           non-codex, non-hermes foreign runtimes
# ===========================================================================

@pytest.mark.parametrize("rt", _FOREIGN_RUNTIMES)
def test_detect_context_window_no_claude_model_for_foreign_runtimes(
    forced_runtime, monkeypatch, clean_ctx_cache, rt):
    """detect_context_window has explicit branches for codex and hermes, then
    falls through to:

        model = os.environ.get("CLAUDE_MODEL", "").lower()
        if not model:
            model = os.environ.get("ANTHROPIC_MODEL", "").lower()

    For cursor / antigravity / grok / opencode / copilot the context window is
    determined by Claude-specific env vars.  If ANTHROPIC_MODEL=claude-sonnet-4
    is set (e.g. from a shared environment on a mixed host), the foreign
    runtime's context window is silently set to the Claude model's window —
    a foreign runtime reading Claude's model settings.
    """
    forced_runtime(rt)
    # Set a Claude model that maps to 1M context.
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    monkeypatch.delenv("CODEX_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("TOKEN_OPTIMIZER_CONTEXT_SIZE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_1M_CONTEXT", raising=False)
    monkeypatch.setattr(measure, "_cli_context_size", None)
    monkeypatch.setattr(measure, "_resolve_feature_env", lambda name: "")
    ctx_window, ctx_source = measure.detect_context_window()
    # If the Claude model was read, ctx_source will mention the model.
    assert "claude" not in ctx_source.lower() and "anthropic" not in ctx_source.lower(), (
        f"detect_context_window for {rt} read ANTHROPIC_MODEL and returned "
        f"ctx_window={ctx_window}, source='{ctx_source}' — "
        f"foreign runtime reading Claude's model settings"
    )


# ===========================================================================
# MEDIUM 6: symlinked runtime home fallback — when CODEX_HOME (or any runtime
#           home env var) is NOT set, the fallback path is returned without
#           any symlink check
# ===========================================================================

def test_symlinked_runtime_home_fallback_not_checked(monkeypatch, tmp_path):
    """_safe_home_from_env only calls _is_safe_home_dir (which rejects symlinks)
    when the env var IS set.  When the env var is NOT set, the fallback
    ``_safe_home() / ".codex"`` is returned directly — no symlink check.

    On a mixed host where ~/.codex is a symlink to ~/.claude (shared config,
    careless admin, or a symlink attack), codex_home() returns ~/.codex which
    resolves to ~/.claude.  All downstream paths (plugin data, snapshots,
    config) then land in the Claude tree — a cross-runtime data leak with no
    env var set and no warning emitted.
    """
    # Build a fake home with .claude as a real dir and .codex as a symlink.
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    claude_dir = fake_home / ".claude"
    claude_dir.mkdir()
    codex_symlink = fake_home / ".codex"
    codex_symlink.symlink_to(claude_dir)

    # Patch _safe_home so the fallback resolves under our temp tree.
    monkeypatch.setattr(runtime_env, "_safe_home", lambda: fake_home)
    # Ensure CODEX_HOME is not set.
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("TOKEN_OPTIMIZER_RUNTIME", raising=False)
    runtime_env.detect_runtime.cache_clear()

    home = runtime_env.codex_home()
    resolved = home.resolve(strict=False)
    assert resolved != claude_dir.resolve(strict=False), (
        f"codex_home() fallback returned a symlink to ~/.claude without "
        f"checking: got {home} which resolves to {resolved} (the Claude "
        f"home). A Codex session would read/write Claude's data tree."
    )


# ===========================================================================
# LOW 7: detect_context_window does NOT strip CODEX_MODEL / OPENAI_MODEL,
#        so whitespace-only values are truthy here but stripped in the
#        quality-curve resolution — the two code paths disagree
# ===========================================================================

def test_detect_context_window_codex_model_not_stripped(monkeypatch, clean_ctx_cache):
    """detect_context_window line 3186:
        model = os.environ.get("CODEX_MODEL") or os.environ.get("OPENAI_MODEL") or …

    No .strip() — so CODEX_MODEL='   ' is truthy and wins, producing a
    whitespace model note.  The quality-curve resolution (line 3452) DOES
    strip, so the two code paths see different models for the same env.
    """
    monkeypatch.setenv("TOKEN_OPTIMIZER_RUNTIME", "codex")
    runtime_env.detect_runtime.cache_clear()
    monkeypatch.setenv("CODEX_MODEL", "   ")
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("TOKEN_OPTIMIZER_CONTEXT_SIZE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_1M_CONTEXT", raising=False)
    monkeypatch.setattr(measure, "_cli_context_size", None)
    monkeypatch.setattr(measure, "_resolve_feature_env", lambda name: "")
    monkeypatch.setattr(measure, "_latest_codex_logged_context_window",
                        lambda: (None, None))
    monkeypatch.setattr(measure, "_codex_config_int", lambda *a: None)
    monkeypatch.setattr(measure, "_codex_config_model", lambda: None)

    _ctx_window, ctx_source = measure.detect_context_window()
    # The whitespace model should NOT appear in the source string.
    assert "for    " not in ctx_source and "for   " not in ctx_source, (
        f"detect_context_window did not strip CODEX_MODEL whitespace; "
        f"ctx_source='{ctx_source}'. The quality-curve path strips it, so "
        f"the two code paths disagree on the model."
    )


# ===========================================================================
# Surface reports: explicit per-surface findings
# ===========================================================================

# Surface: measure.py quality_curve resolution
#   Status: GAP — only codex has the anthropic-default fallback.
#   The 5 other foreign runtimes get "anthropic-default" as their curve label.
#
# Surface: measure.py generate_coach_data
#   Status: GAP — is_codex gate only.  CLAUDE.md / MEMORY.md labels leak to
#   cursor/antigravity/grok/opencode/copilot.
#
# Surface: measure.py generate_auto_recommendations
#   Status: GAP — only codex dispatched to _generate_codex_auto_recommendations.
#   All other foreign runtimes get Claude-specific advice (MEMORY.md, CLAUDE.md,
#   Anthropic recommends, ~/.claude paths).
#
# Surface: detectors/output_waste.py
#   Status: GAP — _instr = "AGENTS.md" if detect_runtime() == "codex" else
#   "CLAUDE.md".  Only codex special-cased.
#
# Surface: detectors/overpowered.py
#   Status: CLEAN — gates on `detect_runtime() not in ("claude", "hermes")`.
#   All foreign runtimes (including the 5 non-codex) are excluded.
#
# Surface: detectors/weak_model.py
#   Status: CLEAN — same gate as overpowered.
#
# Surface: detectors/cache_instability.py
#   Status: CLEAN — same gate as overpowered.
#
# Surface: detectors/respond_to_bash.py
#   Status: CLEAN — gates on `detect_runtime() != "claude"`.
#
# Surface: detectors/wasteful_thinking.py
#   Status: CLEAN (de facto) — no runtime gate, but reads Claude-specific
#   JSONL fields (thinking_tokens) that don't exist in foreign session logs.
#
# Surface: detectors/retry_churn, tool_cascade, looping, bad_decomposition
#   Status: CLEAN — no Claude-specific content in suggestions.
#
# Surface: runtime_env.py detect_runtime lru_cache
#   Status: CLEAN (production) — env is fixed at process start; lru_cache
#   staleness only affects tests, which use cache_clear().
#
# Surface: runtime_env.py symlinked home fallback
#   Status: GAP — fallback path not symlink-checked when env var is unset.
#
# Surface: measure.py detect_context_window
#   Status: GAP — reads CLAUDE_MODEL/ANTHROPIC_MODEL for non-codex, non-hermes
#   foreign runtimes.  Also does not strip CODEX_MODEL/OPENAI_MODEL whitespace.
