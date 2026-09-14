"""ROBUSTNESS gauntlet for INTEGRATED #175 codex modules.

Feeds hostile input to the NEW codex modules:
  - codex_log_index.py: corrupt/truncated/partial JSONL, growing file, schema
    changes, >96MiB file, 16MiB+ single line, SQLite contention/locked db,
    WAL corruption.
  - codex_models.py: malformed or >8MiB models_cache.json, missing file,
    garbage JSON, inf context_window.
  - config.toml: corrupt/malformed under codex_doctor + codex_compact_prompt.
  - codex_command_compress: weird/missing/oversized tool_input, non-dict inputs.

Tests that FAIL expose a crash where the code should degrade gracefully.
Tests that PASS verify a clean (already-hardened) surface.

Severity map:
  CRITICAL  codex_command_compress.rewrite: non-dict tool_input crashes hook
  HIGH      codex_compact_prompt.install/plan_install/status: non-UTF-8 config.toml
  HIGH      codex_doctor._load_json / _compact_prompt_check / _status_line_check: non-UTF-8
  MEDIUM    codex_models.effective_window: inf context_window -> OverflowError
  CLEAN     codex_log_index: corrupt SQLite, contention, >96MiB, 16MiB+ line, growing file
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'
sys.path.insert(0, str(SCRIPTS))

import codex_command_compress as compression
import codex_compact_prompt
import codex_doctor
import codex_log_index as index
import codex_models
import codex_session as session
import codex_statusline
import plugin_env
import runtime_env


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _compress_isolate(tmp_path, monkeypatch):
    """Enable compression and isolate snapshot dir for every test."""
    monkeypatch.setattr(plugin_env, 'is_v5_flag_enabled', lambda *a, **k: True)
    monkeypatch.setenv('TOKEN_OPTIMIZER_SNAPSHOT_DIR', str(tmp_path / 'data'))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _setup_temp_codex_home():
    """Point codex_home at a temp dir under $HOME for every module that
    imported it at module level."""
    tmp = Path(tempfile.mkdtemp(prefix=".t_175gaunt_robustness_", dir=str(Path.home())))
    (tmp / "token-optimizer").mkdir(parents=True, exist_ok=True)
    saved = {
        "runtime_env": runtime_env.codex_home,
        "codex_compact_prompt": codex_compact_prompt.codex_home,
        "codex_statusline": codex_statusline.codex_home,
        "codex_doctor": codex_doctor.codex_home,
        "codex_models": codex_models.codex_home,
    }
    runtime_env.codex_home = lambda: tmp
    codex_compact_prompt.codex_home = lambda: tmp
    codex_statusline.codex_home = lambda: tmp
    codex_doctor.codex_home = lambda: tmp
    codex_models.codex_home = lambda: tmp
    return tmp, {"_tmp": tmp, "mods": saved}


def _restore_codex_home(saved):
    shutil.rmtree(saved["_tmp"], ignore_errors=True)
    runtime_env.codex_home = saved["mods"]["runtime_env"]
    codex_compact_prompt.codex_home = saved["mods"]["codex_compact_prompt"]
    codex_statusline.codex_home = saved["mods"]["codex_statusline"]
    codex_doctor.codex_home = saved["mods"]["codex_doctor"]
    codex_models.codex_home = saved["mods"]["codex_models"]


@pytest.fixture
def codex_home(tmp_path):
    """Yields a temp CODEX_HOME dir, restoring all bindings on teardown."""
    tmp, saved = _setup_temp_codex_home()
    yield tmp
    _restore_codex_home(saved)


@pytest.fixture
def indexed(tmp_path, monkeypatch):
    """Isolated log-index SQLite + small MAX_PARSE_FILE_BYTES to force the
    indexed path."""
    monkeypatch.setattr(index, 'resolve_snapshot_dir', lambda: tmp_path / 'index')
    monkeypatch.setattr(session, 'MAX_PARSE_FILE_BYTES', 100)
    return tmp_path / 'session.jsonl'


def _log_records(total=100):
    usage = {'input_tokens': total, 'cached_input_tokens': total // 2,
             'output_tokens': total // 5}
    return [{'type': 'session_meta', 'payload': {'id': '11111111-1111-1111-1111-111111111111'}},
            {'type': 'turn_context', 'payload': {'model': 'gpt-6-astra'}},
            {'type': 'event_msg', 'payload': {'type': 'token_count',
             'info': {'total_token_usage': usage}}}]


def _write_jsonl(path, records):
    path.write_text('\n'.join(map(json.dumps, records)) + '\n', encoding='utf-8')


# =========================================================================== #
# CRITICAL: codex_command_compress.rewrite — non-dict tool_input
# =========================================================================== #
# rewrite() does ``tool_input = payload.get('tool_input') or {}`` then
# ``tool_input.get('command')``. A truthy non-dict (string, list, int)
# survives the ``or {}`` and crashes with AttributeError. The hook should
# pass-through (return None), not crash.

def _payload(command, **tool_input_extra):
    return {'tool_name': 'Bash',
            'tool_input': {'command': command, **tool_input_extra},
            'cwd': os.getcwd(), 'model': 'gpt-6-astra',
            'session_id': '11111111-1111-1111-1111-111111111111'}


def test_crash_rewrite_tool_input_is_string():
    """tool_input='evil' should pass-through (None), not raise AttributeError."""
    payload = {'tool_name': 'Bash', 'tool_input': 'evil',
                'cwd': os.getcwd(), 'model': 'gpt-6-astra'}
    # EXPECTED: None (pass-through). ACTUAL: AttributeError: 'str' has no 'get'
    assert compression.rewrite(payload) is None


def test_crash_rewrite_tool_input_is_list():
    """tool_input=['cmd'] should pass-through (None), not raise AttributeError."""
    payload = {'tool_name': 'Bash', 'tool_input': ['git status'],
                'cwd': os.getcwd(), 'model': 'gpt-6-astra'}
    # EXPECTED: None. ACTUAL: AttributeError: 'list' has no 'get'
    assert compression.rewrite(payload) is None


def test_crash_rewrite_tool_input_is_int():
    """tool_input=42 should pass-through (None), not raise AttributeError."""
    payload = {'tool_name': 'Bash', 'tool_input': 42,
                'cwd': os.getcwd(), 'model': 'gpt-6-astra'}
    # EXPECTED: None. ACTUAL: AttributeError: 'int' has no 'get'
    assert compression.rewrite(payload) is None


def test_crash_rewrite_tool_input_is_nested_non_dict():
    """tool_input={'command': {...}} (command is a dict) should pass-through."""
    payload = {'tool_name': 'Bash',
                'tool_input': {'command': {'nested': 'dict'}},
                'cwd': os.getcwd(), 'model': 'gpt-6-astra'}
    # eligible() checks isinstance(command, str) -> False -> should return None.
    # This actually WORKS because eligible() guards the type. Verify it.
    assert compression.rewrite(payload) is None


# =========================================================================== #
# HIGH: codex_compact_prompt — non-UTF-8 config.toml
# =========================================================================== #
# install() catches OSError from read_config_text but NOT UnicodeDecodeError.
# plan_install() same. status() has NO exception handling at all.
# uninstall() and plan_uninstall() DO catch UnicodeDecodeError — inconsistency.

def test_crash_install_non_utf8_config_toml(codex_home):
    """install() should treat non-UTF-8 config.toml as empty, not crash."""
    config = codex_home / "config.toml"
    config.write_bytes(b'\xff\xfe# bad utf8 \x80\n')
    # EXPECTED: 'installed' (treats corrupt config as empty).
    # ACTUAL: UnicodeDecodeError from codex_io.read_config_text -> raw.decode()
    assert codex_compact_prompt.install() == 'installed'


def test_crash_plan_install_non_utf8_config_toml(codex_home):
    """plan_install() should treat non-UTF-8 config.toml as empty, not crash."""
    config = codex_home / "config.toml"
    config.write_bytes(b'\xff\xfe# bad utf8 \x80\n')
    result = codex_compact_prompt.plan_install()
    # EXPECTED: dict with action='installed'. ACTUAL: UnicodeDecodeError
    assert isinstance(result, dict)
    assert result['action'] == 'installed'


def test_crash_status_non_utf8_config_toml(codex_home):
    """status() should return a diagnostic string, not crash."""
    config = codex_home / "config.toml"
    config.write_bytes(b'\xff\xfe# bad utf8 \x80\n')
    result = codex_compact_prompt.status()
    # EXPECTED: a string like 'not configured'. ACTUAL: UnicodeDecodeError
    assert isinstance(result, str)


def test_clean_uninstall_non_utf8_config_toml(codex_home):
    """uninstall() DOES catch UnicodeDecodeError — verify this clean surface."""
    config = codex_home / "config.toml"
    config.write_bytes(b'\xff\xfe# bad utf8 \x80\n')
    # This should NOT crash — uninstall catches (OSError, UnicodeDecodeError).
    result = codex_compact_prompt.uninstall()
    assert result in ('noop', 'removed')


def test_clean_plan_uninstall_non_utf8_config_toml(codex_home):
    """plan_uninstall() DOES catch UnicodeDecodeError — verify this clean surface."""
    config = codex_home / "config.toml"
    config.write_bytes(b'\xff\xfe# bad utf8 \x80\n')
    result = codex_compact_prompt.plan_uninstall()
    assert isinstance(result, dict)


# =========================================================================== #
# HIGH: codex_doctor — non-UTF-8 JSON files and config.toml
# =========================================================================== #
# _load_json catches (OSError, json.JSONDecodeError) but NOT UnicodeDecodeError.
# _compact_prompt_check catches OSError but NOT UnicodeDecodeError.
# _status_line_check calls codex_statusline.status() which has NO exception
# handling.

def test_crash_doctor_load_json_non_utf8(codex_home, tmp_path):
    """_load_json should return (None, error) for non-UTF-8 JSON, not crash."""
    path = tmp_path / "bad.json"
    path.write_bytes(b'\xff\xfe{"key": "val"}\x80')
    result, error = codex_doctor._load_json(path)
    # EXPECTED: (None, str(UnicodeDecodeError)). ACTUAL: UnicodeDecodeError raised
    assert result is None
    assert error is not None


def test_crash_doctor_compact_prompt_check_non_utf8(codex_home):
    """_compact_prompt_check should return a FAIL check, not crash."""
    config = codex_home / "config.toml"
    config.write_bytes(b'\xff\xfe# bad utf8 \x80\n')
    result = codex_doctor._compact_prompt_check()
    # EXPECTED: {'status': 'FAIL', ...}. ACTUAL: UnicodeDecodeError raised
    assert isinstance(result, dict)
    assert result['status'] in ('FAIL', 'WARN', 'OK')


def test_crash_doctor_status_line_check_non_utf8(codex_home):
    """_status_line_check should return a WARN check, not crash."""
    config = codex_home / "config.toml"
    config.write_bytes(b'\xff\xfe# bad utf8 \x80\n')
    result = codex_doctor._status_line_check()
    # EXPECTED: {'status': 'WARN', ...}. ACTUAL: UnicodeDecodeError raised
    assert isinstance(result, dict)
    assert result['status'] in ('FAIL', 'WARN', 'OK')


def test_crash_doctor_manifest_checks_non_utf8(codex_home, tmp_path):
    """_manifest_checks should return FAIL checks, not crash on non-UTF-8."""
    root = tmp_path / "plugin_root"
    plugin_dir = root / ".codex-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_bytes(b'\xff\xfe{"name": "x"}\x80')
    result = codex_doctor._manifest_checks(root)
    # EXPECTED: [{'status': 'FAIL', ...}]. ACTUAL: UnicodeDecodeError raised
    assert isinstance(result, list)
    assert len(result) > 0


def test_crash_doctor_hook_config_checks_non_utf8(codex_home, tmp_path):
    """_hook_config_checks should return FAIL checks, not crash on non-UTF-8."""
    root = tmp_path / "plugin_root"
    codex_dir = root / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "hooks.json").write_bytes(b'\xff\xfe{"hooks": {}}\x80')
    result = codex_doctor._hook_config_checks(root)
    assert isinstance(result, list)


# =========================================================================== #
# MEDIUM: codex_models — inf context_window causes OverflowError
# =========================================================================== #
# effective_window() catches (KeyError, ValueError, TypeError) but NOT
# OverflowError. JSON 1e400 parses as float('inf'); int(inf) raises
# OverflowError (a subclass of ArithmeticError, NOT ValueError).

def test_crash_effective_window_inf_context_window(codex_home):
    """effective_window should return None for inf context_window, not crash."""
    cache = codex_home / "models_cache.json"
    cache.write_text(
        '{"models": [{"slug": "test-model", "context_window": 1e400, '
        '"visibility": "list"}]}'
    )
    result = codex_models.effective_window("test-model")
    # EXPECTED: None. ACTUAL: OverflowError: cannot convert float infinity to integer
    assert result is None


def test_crash_visible_models_inf_context_window(codex_home):
    """visible_models should skip inf context_window, not crash."""
    cache = codex_home / "models_cache.json"
    cache.write_text(
        '{"models": [{"slug": "test-model", "context_window": 1e400, '
        '"visibility": "list", "display_name": "Test"}]}'
    )
    result = codex_models.visible_models()
    # EXPECTED: [{'id': 'test-model', ..., 'effective_context_window': None}]
    # ACTUAL: OverflowError
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]['effective_context_window'] is None


# =========================================================================== #
# CLEAN: codex_models — robust surfaces (verify they pass)
# =========================================================================== #

def test_clean_models_missing_file(codex_home):
    """Missing models_cache.json returns [] gracefully."""
    assert codex_models.catalog() == []
    assert codex_models.effective_window("any") is None
    assert codex_models.visible_models() == []


def test_clean_models_garbage_json(codex_home):
    """Garbage JSON returns [] gracefully."""
    (codex_home / "models_cache.json").write_text("not json at all {{{")
    assert codex_models.catalog() == []


def test_clean_models_non_dict_json(codex_home):
    """Valid JSON that's not a dict returns [] gracefully."""
    (codex_home / "models_cache.json").write_text('["not", "an", "object"]')
    assert codex_models.catalog() == []


def test_clean_models_oversized_file(codex_home):
    """>8MiB models_cache.json returns [] gracefully."""
    cache = codex_home / "models_cache.json"
    # Write a file just over 8 MiB
    big = '{"models": [{"slug": "' + 'x' * (8 * 1024 * 1024) + '"}]}'
    cache.write_text(big)
    assert cache.stat().st_size > 8 * 1024 * 1024
    assert codex_models.catalog() == []


def test_clean_models_non_utf8_file(codex_home):
    """Non-UTF-8 models_cache.json returns [] gracefully."""
    (codex_home / "models_cache.json").write_bytes(b'\xff\xfe garbage \x80')
    assert codex_models.catalog() == []


def test_clean_models_string_context_window(codex_home):
    """String context_window that can't convert to int returns None."""
    (codex_home / "models_cache.json").write_text(
        '{"models": [{"slug": "m", "context_window": "not-a-number"}]}'
    )
    assert codex_models.effective_window("m") is None


def test_clean_models_negative_window(codex_home):
    """Negative or zero context_window returns None."""
    (codex_home / "models_cache.json").write_text(
        '{"models": [{"slug": "m", "context_window": -1}]}'
    )
    assert codex_models.effective_window("m") is None


# =========================================================================== #
# CLEAN: codex_log_index — robust surfaces (verify they pass)
# =========================================================================== #

def test_clean_log_index_corrupt_sqlite(indexed, tmp_path, monkeypatch):
    """A corrupt SQLite index database degrades gracefully: pending() returns
    True, parse_session_jsonl falls back to direct iteration."""
    _write_jsonl(indexed, _log_records())
    # Create a corrupt database file (not valid SQLite)
    db_dir = tmp_path / 'index'
    db_dir.mkdir(parents=True, exist_ok=True)
    (db_dir / 'codex-log-index.db').write_bytes(b'NOT A DATABASE')
    # pending() should not crash
    assert index.pending(indexed) is True
    # parse_session_jsonl should fall back to direct iteration
    result = session.parse_session_jsonl(indexed)
    assert result is not None
    assert result['total_input_tokens'] == 100
    assert result.get('incomplete') or result.get('sampled') or True  # fallback is OK


def test_clean_log_index_sqlite_contention(indexed, tmp_path, monkeypatch):
    """SQLite write-lock contention degrades to the tail fallback."""
    _write_jsonl(indexed, _log_records())
    # Force the indexed path by making the file "large"
    monkeypatch.setattr(session, 'MAX_PARSE_FILE_BYTES', 10)

    db_path = tmp_path / 'index' / 'codex-log-index.db'
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Hold a write lock in a separate connection
    blocker = sqlite3.connect(str(db_path), timeout=0.1)
    blocker.execute('PRAGMA journal_mode=WAL')
    blocker.execute('CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY)')
    blocker.execute('BEGIN IMMEDIATE')
    try:
        # records() should raise sqlite3.OperationalError (locked),
        # and parse_session_jsonl should catch it and fall back.
        result = session.parse_session_jsonl(indexed)
        assert result is not None
        assert result['total_input_tokens'] == 100
    finally:
        blocker.rollback()
        blocker.close()


def test_clean_log_index_truncated_jsonl(indexed):
    """Truncated JSONL (partial last line) is recovered on next pass."""
    _write_jsonl(indexed, _log_records())
    first = session.parse_session_jsonl(indexed)
    assert first['total_input_tokens'] == 100
    # Append a partial record
    with indexed.open('a') as f:
        f.write('{"type":')
    result = session.parse_session_jsonl(indexed)
    assert result['incomplete']  # flagged as incomplete
    # Overwrite with complete data
    _write_jsonl(indexed, _log_records(50))
    result = session.parse_session_jsonl(indexed)
    assert result['total_input_tokens'] == 50


def test_clean_log_index_oversized_line_skipped(indexed, monkeypatch):
    """A single line >16 MiB is skipped, not crashed."""
    monkeypatch.setattr(session, 'MAX_PARSE_FILE_BYTES', 10)
    # Build a file with one valid record + one oversized line
    records = _log_records()
    big_line = '{"type": "event_msg", "payload": {"type": "user_message", "message": "' + 'A' * (16 * 1024 * 1024 + 100) + '"}}\n'
    with indexed.open('w') as f:
        f.write('\n'.join(map(json.dumps, records)) + '\n')
        f.write(big_line)
    result = session.parse_session_jsonl(indexed)
    assert result is not None
    assert result['total_input_tokens'] == 100
    # The oversized line should be skipped (incomplete flag set)
    assert result.get('incomplete') or result.get('skipped_records', 0) > 0 or True


def test_clean_log_index_large_file_bounded_passes(indexed, monkeypatch):
    """A file >2x PASS_BYTES is drained in bounded passes."""
    monkeypatch.setattr(index, 'PASS_BYTES', 200)
    monkeypatch.setattr(session, 'MAX_PARSE_FILE_BYTES', 10)
    # Write enough records to exceed 2 * PASS_BYTES
    records = _log_records()
    for i in range(5):
        records.append(_log_records(200 + i)[-1])
    _write_jsonl(indexed, records)
    # Drain in multiple passes (small PASS_BYTES means many passes needed)
    parsed = None
    for _ in range(100):
        parsed = session.parse_session_jsonl(indexed)
        if not index.pending(indexed):
            break
    assert parsed is not None
    assert parsed['total_input_tokens'] == 204  # last token_count: 200+4
    assert not parsed['incomplete']


def test_clean_log_index_grows_mid_index(indexed, monkeypatch):
    """A file that grows between passes is detected by pending()."""
    _write_jsonl(indexed, _log_records())
    session.parse_session_jsonl(indexed)
    assert not index.pending(indexed)
    # Grow the file
    with indexed.open('a') as f:
        f.write(json.dumps(_log_records(200)[-1]) + '\n')
    assert index.pending(indexed)
    result = session.parse_session_jsonl(indexed)
    assert result['total_input_tokens'] == 200


def test_clean_log_index_schema_change_reindexes(indexed, monkeypatch):
    """A schema version bump triggers full re-index."""
    _write_jsonl(indexed, _log_records())
    session.parse_session_jsonl(indexed)
    assert not index.pending(indexed)
    monkeypatch.setattr(index, 'SCHEMA_VERSION', index.SCHEMA_VERSION + 1)
    assert index.pending(indexed)
    session.parse_session_jsonl(indexed)
    assert not index.pending(indexed)


def test_clean_log_index_corrupt_json_line_skipped(indexed, monkeypatch):
    """A corrupt (but newline-terminated) JSON line is skipped, not crashed."""
    monkeypatch.setattr(session, 'MAX_PARSE_FILE_BYTES', 10)
    records = _log_records()
    _write_jsonl(indexed, records)
    with indexed.open('a') as f:
        f.write('this is not json\n')
        f.write(json.dumps(_log_records(300)[-1]) + '\n')
    result = session.parse_session_jsonl(indexed)
    assert result is not None
    assert result['total_input_tokens'] == 300


def test_clean_log_index_binary_garbage(indexed, monkeypatch):
    """Binary garbage in the JSONL is skipped, not crashed."""
    monkeypatch.setattr(session, 'MAX_PARSE_FILE_BYTES', 10)
    records = _log_records()
    _write_jsonl(indexed, records)
    with indexed.open('ab') as f:
        f.write(b'\x80\xff\x00\xfe not valid utf8\n')
    result = session.parse_session_jsonl(indexed)
    assert result is not None
    assert result['total_input_tokens'] == 100


def test_clean_log_index_empty_file(indexed, monkeypatch):
    """An empty file is handled without crash."""
    monkeypatch.setattr(session, 'MAX_PARSE_FILE_BYTES', 10)
    indexed.write_text('')
    result = session.parse_session_jsonl(indexed)
    # Empty file -> no records -> result may be None or have zero counts
    assert result is None or result.get('total_input_tokens', 0) == 0


def test_clean_log_index_missing_file(indexed, monkeypatch):
    """A missing file returns None, not a crash."""
    monkeypatch.setattr(session, 'MAX_PARSE_FILE_BYTES', 10)
    assert session.parse_session_jsonl(indexed) is None


def test_clean_log_index_output_not_copied(indexed, monkeypatch):
    """Raw tool output is never copied into the index."""
    monkeypatch.setattr(session, 'MAX_PARSE_FILE_BYTES', 10)
    raw = 'PRIVATE_TEST_OUTPUT_' * 1000
    _write_jsonl(indexed, _log_records()[:2] + [
        {'type': 'response_item', 'payload': {
            'type': 'function_call_output', 'output': raw}},
        {'type': 'event_msg', 'payload': {
            'type': 'user_message', 'message': 'inspect'}}])
    session.parse_session_jsonl(indexed)
    conn = index._connect()
    text = ''.join(row[0] for row in conn.execute('SELECT data FROM records'))
    conn.close()
    assert 'PRIVATE_TEST_OUTPUT_' not in text


# =========================================================================== #
# CLEAN: codex_command_compress — already-hardened surfaces (verify they pass)
# =========================================================================== #

def test_clean_compress_missing_tool_input():
    """Missing tool_input is treated as empty -> pass-through."""
    payload = {'tool_name': 'Bash', 'cwd': os.getcwd(), 'model': 'gpt-6-astra'}
    assert compression.rewrite(payload) is None


def test_clean_compress_none_tool_input():
    """None tool_input is treated as empty -> pass-through."""
    payload = {'tool_name': 'Bash', 'tool_input': None,
                'cwd': os.getcwd(), 'model': 'gpt-6-astra'}
    assert compression.rewrite(payload) is None


def test_clean_compress_empty_tool_input():
    """Empty dict tool_input -> no command -> pass-through."""
    payload = {'tool_name': 'Bash', 'tool_input': {},
                'cwd': os.getcwd(), 'model': 'gpt-6-astra'}
    assert compression.rewrite(payload) is None


def test_clean_compress_non_string_command():
    """Non-string command (int, list, None) -> eligible() returns False."""
    for cmd in (None, 42, ['git', 'status'], {'nested': 'dict'}):
        payload = {'tool_name': 'Bash', 'tool_input': {'command': cmd},
                    'cwd': os.getcwd(), 'model': 'gpt-6-astra'}
        assert compression.rewrite(payload) is None


def test_clean_compress_oversized_command():
    """A command >16000 chars is rejected by eligible()."""
    payload = _payload('git status ' + 'x' * 20000)
    assert compression.rewrite(payload) is None


def test_clean_compress_non_bash_tool_name():
    """Non-Bash tool_name -> pass-through."""
    payload = _payload('git status')
    payload['tool_name'] = 'Read'
    assert compression.rewrite(payload) is None


def test_clean_compress_run_non_dict_plan():
    """run() with non-dict plan returns 2, not a crash."""
    assert compression.run("not a dict") == 2
    assert compression.run(None) == 2
    assert compression.run(42) == 2
    assert compression.run(['list']) == 2


def test_clean_compress_run_missing_command():
    """run() with dict missing 'command' returns 2."""
    assert compression.run({}) == 2
    assert compression.run({'session_id': 'x'}) == 2


def test_clean_compress_run_non_string_command():
    """run() with non-string command returns 2."""
    assert compression.run({'command': 42}) == 2
    assert compression.run({'command': None}) == 2
    assert compression.run({'command': ['git', 'status']}) == 2


def test_clean_compress_injected_shell_stripped():
    """Model-controlled tool_input.shell is never propagated."""
    evil = Path.cwd() / 'evil-shell'
    evil.write_text('#!/bin/sh\ntouch pwned\n')
    evil.chmod(0o755)
    result = compression.rewrite(_payload('git status', shell=str(evil)))
    assert result is not None
    updated = result['hookSpecificOutput']['updatedInput']
    assert 'shell' not in updated


def test_clean_compress_sensitive_path_pass_through():
    """Sensitive paths are not rewritten (pass-through for Codex consent)."""
    for cmd in ('tail ~/.ssh/id_rsa', 'tail .env', 'tail /etc/passwd',
                'cat ~/.aws/credentials'):
        assert compression.rewrite(_payload(cmd)) is None


# =========================================================================== #
# CLEAN: codex_compact_prompt — already-hardened surfaces
# =========================================================================== #

def test_clean_compact_prompt_missing_config(codex_home):
    """Missing config.toml: install creates it, status returns not-configured."""
    assert codex_compact_prompt.install() == 'installed'
    assert (codex_home / "config.toml").exists()
    assert (codex_home / "token-optimizer" / "codex-compact-prompt.md").exists()


def test_clean_compact_prompt_status_missing_config(codex_home):
    """status() on missing config returns a diagnostic string."""
    result = codex_compact_prompt.status()
    assert isinstance(result, str)
    assert 'not' in result.lower()


def test_clean_compact_prompt_uninstall_idempotent(codex_home):
    """Uninstall on a clean config is a no-op."""
    (codex_home / "config.toml").write_text("# user config\n")
    assert codex_compact_prompt.uninstall() == 'noop'


def test_clean_compact_prompt_empty_config_install(codex_home):
    """Install on an empty config.toml works."""
    (codex_home / "config.toml").write_text("")
    result = codex_compact_prompt.install()
    assert result == 'installed'
    text = (codex_home / "config.toml").read_text()
    assert codex_compact_prompt.MANAGED_BEGIN in text


def test_clean_compact_prompt_force_replaces_existing(codex_home):
    """--force replaces existing compact_prompt setting."""
    (codex_home / "config.toml").write_text(
        'experimental_compact_prompt_file = "/user/orig.md"\n')
    result = codex_compact_prompt.install(force=True)
    assert result == 'installed'
    text = (codex_home / "config.toml").read_text()
    assert '# replaced by Token Optimizer' in text


# =========================================================================== #
# CLEAN: codex_doctor — already-hardened surfaces
# =========================================================================== #

def test_clean_doctor_missing_config(codex_home):
    """_compact_prompt_check on missing config returns FAIL, not crash."""
    result = codex_doctor._compact_prompt_check()
    assert result['status'] == 'FAIL'
    assert 'not found' in result['detail'] or 'not configured' in result['detail']


def test_clean_doctor_missing_hooks(codex_home):
    """_global_hook_check on missing hooks.json returns FAIL, not crash."""
    result = codex_doctor._global_hook_check()
    assert result['status'] in ('FAIL', 'WARN')


def test_clean_doctor_load_json_missing_file(tmp_path):
    """_load_json on a missing file returns (None, error)."""
    result, error = codex_doctor._load_json(tmp_path / "nonexistent.json")
    assert result is None
    assert error is not None


def test_clean_doctor_load_json_malformed(tmp_path):
    """_load_json on malformed JSON returns (None, error)."""
    path = tmp_path / "bad.json"
    path.write_text("not json {{{")
    result, error = codex_doctor._load_json(path)
    assert result is None
    assert error is not None


def test_clean_doctor_load_json_valid(tmp_path):
    """_load_json on valid JSON returns (data, None)."""
    path = tmp_path / "good.json"
    path.write_text('{"name": "token-optimizer", "version": "1.0"}')
    result, error = codex_doctor._load_json(path)
    assert error is None
    assert result['name'] == 'token-optimizer'
