"""Live-state detection must see the browser keyword in the TOOL segment too.

Issue #208: a server that groups many tools names its browser tools by prefix
(mcp__my-tools__browser_click), and the old patterns only matched the keyword
in the server segment (mcp__browser__click). The re-fetch guard then denied a
legitimate repeated click/snapshot — the same args after a scroll are a new
page, not a re-fetch.

These run the real hook script as a subprocess, the way the host does.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

SID = "live-state-segment-session"


def _env(tmp_path, **extra):
    env = {**os.environ, "TOKEN_OPTIMIZER_SNAPSHOT_DIR": str(tmp_path / "snap")}
    env.pop("TOKEN_OPTIMIZER_REFETCH_GUARD_WINDOW_SECONDS", None)
    env.pop("TOKEN_OPTIMIZER_LIVE_STATE_TOOLS", None)
    env.update(extra)
    return env


def _archive_dir(tmp_path):
    return tmp_path / "snap" / "tool-archive" / SID


def _seed_manifest(tmp_path, tool_name, tool_input, tool_use_id, age_seconds=5):
    from refetch_fingerprint import ARGS_HASH_KEY, tool_fingerprint
    d = _archive_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{tool_use_id}.json").write_text(json.dumps({"response": "archived body"}))
    ts = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()
    entry = {"tool_name": tool_name, "tool_use_id": tool_use_id, "tokens_est": 5000,
             ARGS_HASH_KEY: tool_fingerprint(tool_name, tool_input), "timestamp": ts}
    (d / "manifest.jsonl").write_text(json.dumps(entry) + "\n")


def _guard(tmp_path, tool_name, tool_input, **extra):
    p = subprocess.run(
        [sys.executable, str(SCRIPTS / "refetch_guard.py"), "--quiet"],
        input=json.dumps({"hook_event_name": "PreToolUse", "session_id": SID,
                          "tool_name": tool_name, "tool_input": tool_input}),
        capture_output=True, text=True, env=_env(tmp_path, **extra), timeout=60)
    return json.loads(p.stdout.strip())["hookSpecificOutput"].get("permissionDecision")


# --- tool-segment keyword matches ------------------------------------------------

@pytest.mark.parametrize("tool_name", [
    "mcp__my-tools__browser_click",
    "mcp__my-tools__browser_snapshot",
    "mcp__genesis-health__browser_click",
    "mcp__genesis-health__browser_snapshot",
    "mcp__my-tools__chrome_navigate",
    "mcp__my-tools__playwright_fill",
    "mcp__my-tools__puppeteer_goto",
    "mcp__my-tools__computer_use_screenshot",
])
def test_tool_segment_keyword_is_live_state(tool_name):
    from refetch_fingerprint import is_live_state_tool
    assert is_live_state_tool(tool_name), tool_name


@pytest.mark.parametrize("tool_name", [
    "mcp__my-tools__browser_click",
    "mcp__my-tools__browser_snapshot",
])
def test_guard_allows_repeat_of_tool_segment_browser_call(tmp_path, tool_name):
    # The reported bug: an identical browser call on a non-browser server was
    # denied as a re-fetch. A second click after scrolling is new live state.
    args = {"selector": "#submit"}
    _seed_manifest(tmp_path, tool_name, args, "toolu_rep")
    assert _guard(tmp_path, tool_name, args) is None


# --- server-segment matches still work -------------------------------------------

@pytest.mark.parametrize("tool_name", [
    "mcp__browser__click",
    "mcp__claude-in-chrome__computer",
    "mcp__playwright__browser_snapshot",
    "mcp__puppeteer__screenshot",
])
def test_server_segment_keyword_still_live_state(tool_name):
    from refetch_fingerprint import is_live_state_tool
    assert is_live_state_tool(tool_name), tool_name


# --- read tools must stay guarded ------------------------------------------------

@pytest.mark.parametrize("tool_name", [
    "mcp__fs__read_file",
    "mcp__docs__get_page",
    "mcp__somechatty__list_issues",
])
def test_read_tools_are_not_live_state(tool_name):
    from refetch_fingerprint import is_live_state_tool
    assert not is_live_state_tool(tool_name), tool_name


@pytest.mark.parametrize("tool_name", [
    "mcp__fs__read_file",
    "mcp__docs__get_page",
])
def test_guard_still_denies_read_tool_repeats(tmp_path, tool_name):
    args = {"path": "/x"}
    _seed_manifest(tmp_path, tool_name, args, "toolu_guard")
    assert _guard(tmp_path, tool_name, args) == "deny"


# --- env allowlist ----------------------------------------------------------------

def test_env_allowlist_marks_unconventional_tool_live_state(monkeypatch):
    from refetch_fingerprint import is_live_state_tool
    monkeypatch.setenv("TOKEN_OPTIMIZER_LIVE_STATE_TOOLS",
                       "mcp__acme__screen*, mcp__acme__scrape_*")
    assert is_live_state_tool("mcp__acme__screen_grab")
    assert is_live_state_tool("mcp__acme__scrape_now")
    assert not is_live_state_tool("mcp__acme__read_file")


def test_env_allowlist_lets_guard_allow_repeat(tmp_path):
    args = {"region": "full"}
    _seed_manifest(tmp_path, "mcp__acme__screen_grab", args, "toolu_env")
    assert _guard(tmp_path, "mcp__acme__screen_grab", args,
                  TOKEN_OPTIMIZER_LIVE_STATE_TOOLS="mcp__acme__screen*") is None


def test_bad_env_value_fails_open(monkeypatch):
    from refetch_fingerprint import is_live_state_tool
    for bad in ("[unclosed", "???,,,", "(*", " , , "):
        monkeypatch.setenv("TOKEN_OPTIMIZER_LIVE_STATE_TOOLS", bad)
        # Builtins still classify; a plain read tool still doesn't.
        assert is_live_state_tool("mcp__browser__click"), bad
        assert is_live_state_tool("mcp__my-tools__browser_click"), bad
        assert not is_live_state_tool("mcp__fs__read_file"), bad


def test_unset_env_does_not_change_read_tool_guarding(tmp_path):
    args = {"path": "/x"}
    _seed_manifest(tmp_path, "mcp__fs__read_file", args, "toolu_noenv")
    assert _guard(tmp_path, "mcp__fs__read_file", args) == "deny"
