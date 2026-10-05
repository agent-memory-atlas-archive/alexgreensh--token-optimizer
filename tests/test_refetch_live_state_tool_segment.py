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


@pytest.fixture(autouse=True)
def _fresh_env_patterns():
    """The allowlist is parsed once per process; drop the cache so a test's
    own env change (or a previous test's) is honoured in-process too."""
    import refetch_fingerprint
    refetch_fingerprint._LIVE_STATE_PATTERNS_CACHE = (
        refetch_fingerprint._LIVE_STATE_PATTERNS_UNSET
    )
    yield
    refetch_fingerprint._LIVE_STATE_PATTERNS_CACHE = (
        refetch_fingerprint._LIVE_STATE_PATTERNS_UNSET
    )


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
    # The residual keyword families from report A finding 4 / report B
    # finding 1: browse (no 'r'), chromium/devtools spellings, computeruse
    # with no separator, and tool-segment observer verbs.
    "mcp__my-tools__browse_page",
    "mcp__x__browse_docs",
    "mcp__my-tools__chromium_launch",
    "mcp__devtools-panel__inspect",
    "mcp__x__computeruse",
    "mcp__x__take_screenshot",
    "mcp__x__page_snapshot",
    "mcp__x__get_page_state",
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
    "mcp__browse__open_page",
    "mcp__chromium__click",
    "mcp__devtools__snapshot",
    "mcp__computeruse__type",
])
def test_server_segment_keyword_still_live_state(tool_name):
    from refetch_fingerprint import is_live_state_tool
    assert is_live_state_tool(tool_name), tool_name


# --- read tools must stay guarded ------------------------------------------------

# Real tool names from popular non-browser MCP servers: filesystem, github,
# notion, context7, tavily, brightdata. Every one must remain guarded.
@pytest.mark.parametrize("tool_name", [
    "mcp__fs__read_file",
    "mcp__docs__get_page",
    "mcp__somechatty__list_issues",
    # @modelcontextprotocol/server-filesystem
    "mcp__filesystem__read_file",
    "mcp__filesystem__read_text_file",
    "mcp__filesystem__list_directory",
    "mcp__filesystem__search_files",
    # github-mcp-server
    "mcp__github__get_file_contents",
    "mcp__github__search_code",
    "mcp__github__search_repositories",
    "mcp__github__list_issues",
    "mcp__github__create_pull_request",
    # notion
    "mcp__notion__API-post-search",
    "mcp__notion__API-retrieve-a-page",
    "mcp__notion__API-query-database",
    # context7
    "mcp__context7__resolve-library-id",
    "mcp__context7__get-library-docs",
    "mcp__context7__query-docs",
    # tavily
    "mcp__tavily__tavily-search",
    "mcp__tavily__tavily-extract",
    "mcp__tavily__search",
    # brightdata (note: its scraping_browser_* tools ARE live-state; these are not)
    "mcp__brightdata__search_engine",
    "mcp__brightdata__scrape_as_markdown",
    "mcp__brightdata__web_data_amazon_product",
    "mcp__brightdata__session_stats",
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


# --- the real writer path: live-state tools get args_hash=None -------------------

def _write_archive(tmp_path, tool_name, tool_input, tool_use_id="toolu_written"):
    """Drive the real PostToolUse archive writer and return the manifest entry."""
    payload = {"hook_event_name": "PostToolUse", "session_id": SID,
               "tool_name": tool_name, "tool_use_id": tool_use_id,
               "tool_input": tool_input,
               "tool_response": "page text\n" * 2000}
    p = subprocess.run(
        [sys.executable, str(SCRIPTS / "archive_result.py"), "--quiet"],
        input=json.dumps(payload), capture_output=True, text=True,
        env=_env(tmp_path), timeout=60)
    assert p.returncode == 0, f"archive_result failed: {p.stderr[-2000:]}"
    manifest = _archive_dir(tmp_path) / "manifest.jsonl"
    assert manifest.exists(), f"no manifest written: {p.stdout[:400]}"
    entries = [json.loads(l) for l in manifest.read_text().splitlines() if l.strip()]
    (entry,) = [e for e in entries if e.get("tool_use_id") == tool_use_id]
    return entry


def test_writer_stores_no_fingerprint_for_live_state_tool(tmp_path):
    # The writer's half of the fix: live-state tools must not get an
    # args_hash, so old manifests can never deny a live-state repeat.
    from refetch_fingerprint import ARGS_HASH_KEY
    entry = _write_archive(tmp_path, "mcp__my-tools__browser_click",
                           {"selector": "#submit"})
    assert entry.get(ARGS_HASH_KEY) is None, (
        "live-state tool got an args_hash — the writer must not fingerprint "
        "tools whose result is live state"
    )


def test_writer_still_fingerprints_a_read_tool(tmp_path):
    from refetch_fingerprint import ARGS_HASH_KEY, tool_fingerprint
    args = {"path": "/x"}
    entry = _write_archive(tmp_path, "mcp__fs__read_file", args)
    assert entry.get(ARGS_HASH_KEY) == tool_fingerprint("mcp__fs__read_file", args)


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


def test_settings_json_env_block_marks_tool_live_state(tmp_path):
    """Like its sibling TOKEN_OPTIMIZER_ARCHIVE_EXEMPT_TOOLS, the allowlist is
    also read from the settings.json env block — for hosts that never inject
    it into the hook subprocess environment."""
    claude = tmp_path / "claude"
    claude.mkdir()
    (claude / "settings.json").write_text(json.dumps(
        {"env": {"TOKEN_OPTIMIZER_LIVE_STATE_TOOLS": "mcp__acme__screen*"}}))
    args = {"region": "full"}
    _seed_manifest(tmp_path, "mcp__acme__screen_grab", args, "toolu_settings")
    assert _guard(tmp_path, "mcp__acme__screen_grab", args,
                  CLAUDE_CONFIG_DIR=str(claude)) is None


def test_process_env_beats_settings_json_env_block(tmp_path, monkeypatch):
    """Process env wins over settings.json, matching the sibling loader."""
    from refetch_fingerprint import is_live_state_tool
    claude = tmp_path / "claude"
    claude.mkdir()
    (claude / "settings.json").write_text(json.dumps(
        {"env": {"TOKEN_OPTIMIZER_LIVE_STATE_TOOLS": "mcp__acme__screen*"}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("TOKEN_OPTIMIZER_LIVE_STATE_TOOLS", "mcp__acme__fetch*")
    assert is_live_state_tool("mcp__acme__fetch_now")
    assert not is_live_state_tool("mcp__acme__screen_grab")


def test_env_allowlist_is_capped(monkeypatch):
    """A pathological value cannot multiply per-call latency: only the first
    _LIVE_STATE_TOOLS_MAX_PATTERNS patterns are honoured."""
    import refetch_fingerprint
    cap = refetch_fingerprint._LIVE_STATE_TOOLS_MAX_PATTERNS
    patterns = ",".join(["mcp__x__nope_*"] * cap + ["mcp__x__past_cap*"])
    monkeypatch.setenv("TOKEN_OPTIMIZER_LIVE_STATE_TOOLS", patterns)
    assert refetch_fingerprint.is_live_state_tool("mcp__x__nope_1")
    assert not refetch_fingerprint.is_live_state_tool("mcp__x__past_cap_y")


def test_env_allowlist_is_parsed_once_per_process(monkeypatch):
    """Parsed once per process: changing the env mid-process must NOT change
    classification (a hook subprocess always has a fixed env anyway)."""
    import refetch_fingerprint
    monkeypatch.setenv("TOKEN_OPTIMIZER_LIVE_STATE_TOOLS", "mcp__acme__screen*")
    assert refetch_fingerprint.is_live_state_tool("mcp__acme__screen_grab")
    monkeypatch.setenv("TOKEN_OPTIMIZER_LIVE_STATE_TOOLS", "mcp__other__*")
    assert refetch_fingerprint.is_live_state_tool("mcp__acme__screen_grab")
    assert not refetch_fingerprint.is_live_state_tool("mcp__other__tool")


def test_bad_env_value_fails_open(monkeypatch):
    import refetch_fingerprint
    for bad in ("[unclosed", "???,,,", "(*", " , , "):
        monkeypatch.setenv("TOKEN_OPTIMIZER_LIVE_STATE_TOOLS", bad)
        refetch_fingerprint._LIVE_STATE_PATTERNS_CACHE = (
            refetch_fingerprint._LIVE_STATE_PATTERNS_UNSET
        )
        # Builtins still classify; a plain read tool still doesn't.
        assert refetch_fingerprint.is_live_state_tool("mcp__browser__click"), bad
        assert refetch_fingerprint.is_live_state_tool("mcp__my-tools__browser_click"), bad
        assert not refetch_fingerprint.is_live_state_tool("mcp__fs__read_file"), bad


def test_unset_env_does_not_change_read_tool_guarding(tmp_path):
    args = {"path": "/x"}
    _seed_manifest(tmp_path, "mcp__fs__read_file", args, "toolu_noenv")
    assert _guard(tmp_path, "mcp__fs__read_file", args) == "deny"
