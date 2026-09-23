"""The Cowork build stands down on a desktop host that already runs the standard
Token Optimizer, so account-synced installs never fire every hook twice.

Covers: stand-down with the standard plugin enabled, stand-down with a script
install (hooks in settings.json, POSIX and Windows paths), no stand-down inside
Cowork (either host env signal), no stand-down for the standard plugin itself,
no stand-down when the standard plugin is absent or disabled, fail-open on
missing or malformed settings, and that main() exits before dispatching.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks"
if str(HOOKS) not in sys.path:
    sys.path.insert(0, str(HOOKS))

import run  # noqa: E402


def _set_home(monkeypatch, path):
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("USERPROFILE", str(path))


@pytest.fixture()
def host(tmp_path, monkeypatch):
    """Desktop host: no Cowork env, HOME redirected, no CLAUDE_CONFIG_DIR."""
    for var in ("CLAUDE_CODE_REMOTE", "CLAUDE_CODE_CONTAINER_ID", "CLAUDE_CONFIG_DIR"):
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    _set_home(monkeypatch, home)
    return home


def _plugin(tmp_path, monkeypatch, name):
    root = tmp_path / "synced" / name
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    return root


def _settings(home, data):
    (home / ".claude" / "settings.json").write_text(
        data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


STANDARD_ON = {"enabledPlugins": {"token-optimizer@alexgreensh-token-optimizer": True}}


def test_cowork_build_stands_down_when_standard_plugin_enabled(host, tmp_path, monkeypatch):
    _plugin(tmp_path, monkeypatch, "token-optimizer-cowork")
    _settings(host, STANDARD_ON)
    assert run._cowork_copy_should_stand_down() is True


@pytest.mark.parametrize("command", [
    "python3 /Users/a/.claude/skills/token-optimizer/scripts/measure.py ensure-health",
    "python C:\\Users\\a\\.claude\\skills\\token-optimizer\\scripts\\measure.py ensure-health",
])
def test_cowork_build_stands_down_for_script_install(host, tmp_path, monkeypatch, command):
    _plugin(tmp_path, monkeypatch, "token-optimizer-cowork")
    _settings(host, {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}})
    assert run._cowork_copy_should_stand_down() is True


@pytest.mark.parametrize("env,value", [("CLAUDE_CODE_REMOTE", "1"), ("CLAUDE_CODE_CONTAINER_ID", "abc123")])
def test_cowork_build_runs_inside_cowork(host, tmp_path, monkeypatch, env, value):
    _plugin(tmp_path, monkeypatch, "token-optimizer-cowork")
    _settings(host, STANDARD_ON)
    monkeypatch.setenv(env, value)
    assert run._cowork_copy_should_stand_down() is False


def test_standard_plugin_never_stands_down(host, tmp_path, monkeypatch):
    _plugin(tmp_path, monkeypatch, "token-optimizer")
    _settings(host, STANDARD_ON)
    assert run._cowork_copy_should_stand_down() is False


@pytest.mark.parametrize("settings", [
    {"enabledPlugins": {"token-optimizer@alexgreensh-token-optimizer": False}},
    {"enabledPlugins": {"token-optimizer-cowork@synced": True, "other@market": True}},
    {},
])
def test_cowork_build_runs_without_an_active_standard_install(host, tmp_path, monkeypatch, settings):
    _plugin(tmp_path, monkeypatch, "token-optimizer-cowork")
    _settings(host, settings)
    assert run._cowork_copy_should_stand_down() is False


@pytest.mark.parametrize("raw", [None, "{not json", "[]"])
def test_fail_open_on_missing_or_malformed_settings(host, tmp_path, monkeypatch, raw):
    _plugin(tmp_path, monkeypatch, "token-optimizer-cowork")
    if raw is not None:
        _settings(host, raw)
    assert run._cowork_copy_should_stand_down() is False


def test_main_exits_before_dispatch_when_standing_down(host, tmp_path, monkeypatch):
    root = _plugin(tmp_path, monkeypatch, "token-optimizer-cowork")
    (root / "hooks").mkdir()
    (root / "hooks" / "stop_runner.py").write_text("", encoding="utf-8")
    _settings(host, STANDARD_ON)
    monkeypatch.setattr(sys, "argv", ["run.py", "hooks/stop_runner.py"])

    def _no_dispatch(*a, **k):
        raise AssertionError("hook dispatched while the standard install is active")

    monkeypatch.setattr(run.subprocess, "Popen", _no_dispatch)
    monkeypatch.setattr(run.subprocess, "run", _no_dispatch)
    assert run.main() == 0
