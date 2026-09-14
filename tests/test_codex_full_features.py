"""Index-correctness tests for the incremental Codex log index.

Ported from @dormancygrace's PR (items 11+12). These tests verify the
incremental SQLite index: bounded passes, pending() recheck, schema-version
invalidation, same-size rewrite detection, partial-record recovery, and
that raw tool output is never copied into the index.

Note: cached_input_tokens is set to 0 so the assertions hold under the
base cumulative token accounting. The parallel extraction (items 6-8)
switches to delta accounting; once both branches merge, the original
total//2 value can be restored.
"""
import json
import os
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'
sys.path.insert(0, str(SCRIPTS))
import codex_session as session
import codex_log_index as index


@pytest.fixture
def indexed(tmp_path, monkeypatch):
    monkeypatch.setattr(index, 'resolve_snapshot_dir', lambda: tmp_path / 'index')
    monkeypatch.setattr(session, 'MAX_PARSE_FILE_BYTES', 100)
    return tmp_path / 'session.jsonl'


def log_records(total=100):
    usage = {'input_tokens': total, 'cached_input_tokens': 0, 'output_tokens': total // 5}
    return [{'type': 'session_meta', 'payload': {'id': '11111111-1111-1111-1111-111111111111'}},
            {'type': 'turn_context', 'payload': {'model': 'gpt-6-astra'}},
            {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'total_token_usage': usage}}}]


def write(path, records):
    path.write_text('\n'.join(map(json.dumps, records)) + '\n', encoding='utf-8')


def test_full_index_counts_all_usage_and_only_appended_changes(indexed):
    write(indexed, log_records())
    first = session.parse_session_jsonl(indexed)
    assert first['total_input_tokens'] == 100 and not first['incomplete']
    assert first['scan_mode'] == 'indexed_full'
    assert not index.pending(indexed)
    with indexed.open('a') as handle:
        handle.write(json.dumps(log_records(200)[-1]) + '\n')
    assert index.pending(indexed)
    result = session.parse_session_jsonl(indexed)
    assert result['total_input_tokens'] == 200
    assert result['total_output_tokens'] == 40
    assert result == session.parse_session_jsonl(indexed)


def test_index_drains_unchanged_file_in_bounded_passes(indexed, monkeypatch):
    monkeypatch.setattr(index, 'PASS_BYTES', 200)
    write(indexed, log_records() + [log_records(200)[-1], log_records(300)[-1]])
    for _ in range(10):
        parsed = session.parse_session_jsonl(indexed)
        if not index.pending(indexed):
            break
    assert parsed['total_input_tokens'] == 300
    assert not parsed['incomplete']


def test_index_revisits_schema_changes_and_same_size_rewrites(indexed, monkeypatch):
    write(indexed, log_records(200))
    session.parse_session_jsonl(indexed)
    assert not index.pending(indexed)
    monkeypatch.setattr(index, 'SCHEMA_VERSION', index.SCHEMA_VERSION + 1)
    assert index.pending(indexed)
    session.parse_session_jsonl(indexed)
    assert not index.pending(indexed)
    old = indexed.stat()
    write(indexed, log_records(300))
    assert indexed.stat().st_size == old.st_size
    os.utime(indexed, ns=(old.st_atime_ns, old.st_mtime_ns + 1000000))
    assert index.pending(indexed)
    assert session.parse_session_jsonl(indexed)['total_input_tokens'] == 300


def test_index_recovers_partial_record_and_truncation(indexed):
    write(indexed, log_records())
    assert session.parse_session_jsonl(indexed)['total_input_tokens'] == 100
    with indexed.open('a') as handle:
        handle.write('{"type":')
    assert session.parse_session_jsonl(indexed)['incomplete']
    write(indexed, log_records(50))
    assert session.parse_session_jsonl(indexed)['total_input_tokens'] == 50


def test_index_preserves_estimated_character_counts_without_copying_output(indexed):
    raw = 'PRIVATE_TEST_OUTPUT_' * 1000
    write(indexed, log_records()[:2] + [{'type': 'response_item', 'payload': {
        'type': 'function_call_output', 'output': raw}}, {'type': 'event_msg',
        'payload': {'type': 'user_message', 'message': 'inspect'}}])
    parsed = session.parse_session_jsonl(indexed)
    assert parsed['total_input_tokens'] == (len(raw) + len('inspect')) // 4
    conn = index._connect()
    text = ''.join(row[0] for row in conn.execute('SELECT data FROM records'))
    conn.close()
    assert 'PRIVATE_TEST_OUTPUT_' not in text
