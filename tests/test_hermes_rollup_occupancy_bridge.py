from __future__ import annotations
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import hermes_hook_bridge as bridge  # noqa: E402


def test_rollup_forwards_latest_prompt_tokens(monkeypatch, tmp_path):
    measure = tmp_path / "measure.py"
    measure.write_text("# stub\n", encoding="utf-8")
    monkeypatch.setattr(bridge, "_locate_measure_py", lambda: measure)
    calls = []
    monkeypatch.setattr(bridge, "spawn_detached", lambda cmd, **kwargs: calls.append(cmd) or object())
    bridge.run_rollup("session-1", reason="done", context_tokens=278_545)
    rollup = calls[0]
    assert rollup[rollup.index("--context-tokens") + 1] == "278545"
    assert rollup[rollup.index("--session") + 1] == "session-1"


def test_rollup_omits_unknown_occupancy(monkeypatch, tmp_path):
    measure = tmp_path / "measure.py"
    measure.write_text("# stub\n", encoding="utf-8")
    monkeypatch.setattr(bridge, "_locate_measure_py", lambda: measure)
    calls = []
    monkeypatch.setattr(bridge, "spawn_detached", lambda cmd, **kwargs: calls.append(cmd) or object())
    bridge.run_rollup("session-2")
    assert "--context-tokens" not in calls[0]


def test_rollup_omits_non_positive_occupancy(monkeypatch, tmp_path):
    """Zero/negative context_tokens are not real prompt sizes — a session that
    never recorded a prompt must stay 'unavailable', not ship a fake reading."""
    measure = tmp_path / "measure.py"
    measure.write_text("# stub\n", encoding="utf-8")
    monkeypatch.setattr(bridge, "_locate_measure_py", lambda: measure)
    calls = []
    monkeypatch.setattr(bridge, "spawn_detached", lambda cmd, **kwargs: calls.append(cmd) or object())
    bridge.run_rollup("session-3", context_tokens=0)
    bridge.run_rollup("session-4", context_tokens=-50)
    rollups = [cmd for cmd in calls if "hermes-rollup" in cmd]
    assert len(rollups) == 2
    assert all("--context-tokens" not in cmd for cmd in rollups)
