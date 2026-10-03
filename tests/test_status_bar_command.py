"""`measure.py status-bar --session <id> --json`: the desktop band's one read.

Plan U2 / KTD5. One spawn returns savings (this session, 30 local days by day,
the 30-day headline), the last main-thread request time and measured cache
lifetime from the transcript, and the checkpoint saved time from the freshest
quality cache. Savings are answered from a per-session JSON cache under
SNAPSHOT_DIR; a cache older than 60 s starts exactly one detached refresh.

Run: python3 -m pytest tests/test_status_bar_command.py -q
"""
import importlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "token-optimizer" / "scripts"
MEASURE = SCRIPTS / "measure.py"

SID_A = "aaaaaaaa-1111-2222-3333-444444444444"
SID_B = "bbbbbbbb-1111-2222-3333-444444444444"


def _sandbox_env(tmp_path):
    snap = tmp_path / "snap"
    home = tmp_path / "home"
    claude = home / ".claude"
    for d in (snap, claude / "projects" / "-proj", claude / "token-optimizer"):
        d.mkdir(parents=True, exist_ok=True)
    env = {
        "TOKEN_OPTIMIZER_SNAPSHOT_DIR": str(snap),
        "CLAUDE_CONFIG_DIR": str(claude),
        "HOME": str(home),
        "USERPROFILE": str(home),
    }
    return env, snap, claude


@pytest.fixture()
def sb(tmp_path, monkeypatch):
    env, snap, claude = _sandbox_env(tmp_path)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    sys.path.insert(0, str(SCRIPTS))
    sys.modules.pop("measure", None)
    m = importlib.import_module("measure")
    m._sb_env = env
    m._sb_snap = snap
    m._sb_claude = claude
    yield m
    sys.modules.pop("measure", None)


def _day(offset_days, hour=12):
    d = datetime.now().replace(hour=hour, minute=0, second=0, microsecond=0)
    return (d - timedelta(days=offset_days))


def _seed(m, rows=None, compression=None):
    conn = m._init_trends_db()
    try:
        for ts, etype, tok, cost, sid in rows or []:
            conn.execute(
                "INSERT INTO savings_events (timestamp, event_type, tokens_saved, "
                "cost_saved_usd, session_id, session_uuid) VALUES (?,?,?,?,?,?)",
                (ts.isoformat(), etype, tok, cost, sid, sid))
        for ts, feature, orig, comp, sid, tier in compression or []:
            conn.execute(
                "INSERT INTO compression_events (timestamp, session_id, session_uuid, "
                "feature, original_tokens, compressed_tokens, model, tier) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (ts.isoformat(), sid, sid, feature, orig, comp, "claude-opus-4-5", tier))
        conn.commit()
    finally:
        conn.close()


def _three_day_fixture(m):
    _seed(m, rows=[
        (_day(0), "tool_archive", 1000, 0.01, SID_A),
        (_day(2), "tool_archive", 2000, 0.02, SID_A),
        (_day(5), "structure_map", 3000, 0.03, SID_A),
        (_day(1), "tool_archive", 5000, 0.05, SID_B),
        # Estimated tier: relocated out of the realized figures, like the headline.
        (_day(0), "mcp_cap", 9999, 0.99, SID_A),
        # Older than 30 days: in neither the bars nor the 30-day total.
        (_day(40), "tool_archive", 70000, 0.70, SID_B),
    ], compression=[
        (_day(0), "bash_compress_git", 600, 100, SID_A, "measured"),
        # Opportunity tier never counts.
        (_day(0), "bash_compress_git", 900, 0, SID_A, "opportunity"),
    ])


def _by_date(daily):
    return {d["date"]: d for d in daily}


# --------------------------------------------------------------------------
# Savings figures
# --------------------------------------------------------------------------

def test_three_days_two_sessions(sb):
    _three_day_fixture(sb)
    sav = sb._status_bar_compute_savings(SID_A)
    assert sav is not None
    assert sav["unit"] == "tokens"
    # Session A: 1000 + 2000 + 3000 realized + 500 compression; mcp_cap excluded.
    assert sav["session_tokens"] == 6500
    daily = sav["daily"]
    assert len(daily) == 30
    dates = [d["date"] for d in daily]
    assert dates == sorted(dates)
    assert dates[-1] == datetime.now().date().isoformat()
    by = _by_date(daily)
    assert by[_day(0).date().isoformat()]["tokens"] == 1500
    assert by[_day(1).date().isoformat()]["tokens"] == 5000
    assert by[_day(2).date().isoformat()]["tokens"] == 2000
    assert by[_day(5).date().isoformat()]["tokens"] == 3000
    zero_days = [d for d in daily if d["date"] not in {
        _day(k).date().isoformat() for k in (0, 1, 2, 5)}]
    assert len(zero_days) == 26
    assert all(d["tokens"] == 0 and d["usd"] == 0 for d in zero_days)
    headline = sb._get_merged_savings(days=30)
    assert sav["total_30d_usd"] == headline["total_cost_usd"]
    assert sav["total_30d_tokens"] == headline["total_tokens"]


def test_rows_older_than_30_days_excluded(sb):
    _three_day_fixture(sb)
    sav = sb._status_bar_compute_savings(SID_B)
    assert sum(d["tokens"] for d in sav["daily"]) == 11500
    assert _day(40).date().isoformat() not in _by_date(sav["daily"])
    # The 70,000-token row 40 days back is outside the 30-day headline too.
    assert sav["total_30d_tokens"] < 70000
    assert sav["total_30d_tokens"] == sb._get_merged_savings(days=30)["total_tokens"]


def test_total_equals_merged_savings_headline(sb):
    _three_day_fixture(sb)
    sav = sb._status_bar_compute_savings(SID_A)
    assert sav["total_30d_usd"] == sb._get_merged_savings(days=30)["total_cost_usd"]


def test_savings_summary_still_matches_realized_helper(sb):
    """The window total and the bars share one netting helper."""
    _seed(sb, rows=[
        (_day(0), "tool_archive", 4000, 0.04, SID_A),
        (_day(0), "tool_archive_reexpand", 1500, 0.015, SID_A),
        (_day(0), "verbosity_steer", 800, 0.008, SID_A),
    ])
    summary = sb._get_savings_summary(days=30)
    assert summary["total_tokens"] == 2500
    sav = sb._status_bar_compute_savings(SID_A)
    assert sav["session_tokens"] == 2500


def test_missing_trends_db_returns_null_savings_exit_0(sb, tmp_path):
    env = dict(os.environ)
    env.update(sb._sb_env)
    assert not (sb._sb_snap / "trends.db").exists()
    proc = subprocess.run(
        [sys.executable, str(MEASURE), "status-bar", "--session", SID_A, "--json"],
        capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["savings"] is None
    assert out["savings_state"] == "unavailable"
    assert out["savings_reason"]
    assert out["last_request_epoch"] is None
    assert out["cache_lifetime"] is None
    assert out["last_checkpoint_epoch"] is None
    assert not (sb._sb_snap / "trends.db").exists(), "a read must not create the DB"


# --------------------------------------------------------------------------
# Transcript: last main-thread request and measured lifetime
# --------------------------------------------------------------------------

def _assistant(ts, usage, sidechain=False, model="claude-opus-4-5"):
    rec = {"type": "assistant", "timestamp": ts,
           "message": {"model": model, "usage": usage}}
    if sidechain:
        rec["isSidechain"] = True
        rec["agentId"] = "agent-1"
    return json.dumps(rec)


def _write_transcript(sb, lines, sid=SID_A):
    p = sb._sb_claude / "projects" / "-proj" / f"{sid}.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_subagent_last_row_returns_main_thread_time(sb):
    p = _write_transcript(sb, [
        _assistant("2026-10-03T10:00:00.000Z",
                   {"input_tokens": 5, "cache_creation": {"ephemeral_5m_input_tokens": 900}}),
        json.dumps({"type": "user", "timestamp": "2026-10-03T10:00:30.000Z",
                    "message": {"content": "hi"}}),
        _assistant("2026-10-03T10:05:00.000Z",
                   {"input_tokens": 5, "cache_creation": {"ephemeral_1h_input_tokens": 50}},
                   sidechain=True),
    ])
    ts, lifetime = sb._status_bar_transcript_state(p)
    assert ts == datetime.fromisoformat("2026-10-03T10:00:00+00:00").timestamp()
    assert lifetime == "5m"


def test_one_hour_write_then_reads_returns_1h(sb):
    p = _write_transcript(sb, [
        _assistant("2026-10-03T10:00:00Z",
                   {"cache_creation": {"ephemeral_1h_input_tokens": 40000}}),
        _assistant("2026-10-03T10:01:00Z", {"cache_read_input_tokens": 40000}),
        _assistant("2026-10-03T10:02:00Z", {"cache_read_input_tokens": 40100}),
    ])
    ts, lifetime = sb._status_bar_transcript_state(p)
    assert lifetime == "1h"
    assert ts == datetime.fromisoformat("2026-10-03T10:02:00+00:00").timestamp()


def test_command_reads_transcript_by_session_id(sb):
    _write_transcript(sb, [
        _assistant("2026-10-03T10:00:00Z",
                   {"cache_creation": {"ephemeral_1h_input_tokens": 10}}),
    ])
    out = sb.status_bar_payload(SID_A)
    assert out["cache_lifetime"] == "1h"
    assert out["last_request_epoch"] == datetime.fromisoformat(
        "2026-10-03T10:00:00+00:00").timestamp()


# --------------------------------------------------------------------------
# Checkpoint saved time: freshest quality cache across storage dirs
# --------------------------------------------------------------------------

def test_checkpoint_epoch_from_freshest_quality_cache(sb):
    old = sb._sb_claude / "token-optimizer" / f"quality-cache-{SID_A}.json"
    old.write_text(json.dumps({"last_checkpoint_epoch": 1000}), encoding="utf-8")
    os.utime(old, (time.time() - 600, time.time() - 600))
    plugin_dir = (sb._sb_claude / "plugins" / "data"
                  / "token-optimizer-alexgreensh-token-optimizer" / "token-optimizer")
    plugin_dir.mkdir(parents=True)
    (plugin_dir / f"quality-cache-{SID_A}.json").write_text(
        json.dumps({"last_checkpoint_epoch": 2000}), encoding="utf-8")
    assert sb._status_bar_checkpoint_epoch(SID_A) == 2000


def test_earlier_checkpoint_from_resumable_flag(sb):
    cp = sb._sb_claude / "token-optimizer" / "checkpoints" / "99999999-aaaa-20261003-120001-stop.md"
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text("# checkpoint", encoding="utf-8")
    flag = sb._sb_claude / "token-optimizer" / f"resumable-{SID_A}.json"
    flag.write_text(json.dumps({"checkpoint": str(cp), "ts": 1}), encoding="utf-8")
    got = sb._status_bar_earlier_checkpoint(SID_A)
    assert got is not None and got["epoch"] == int(cp.stat().st_mtime)
    assert "about" in got


def test_earlier_checkpoint_none_without_flag_or_for_own_checkpoint(sb):
    assert sb._status_bar_earlier_checkpoint(SID_A) is None
    own = sb._sb_claude / "token-optimizer" / "checkpoints" / f"{SID_A}-20261003-120001-stop.md"
    own.parent.mkdir(parents=True, exist_ok=True)
    own.write_text("# mine", encoding="utf-8")
    (sb._sb_claude / "token-optimizer" / f"resumable-{SID_A}.json").write_text(
        json.dumps({"checkpoint": str(own)}), encoding="utf-8")
    assert sb._status_bar_earlier_checkpoint(SID_A) is None


# --------------------------------------------------------------------------
# Cache and refresh (KTD5)
# --------------------------------------------------------------------------

def test_cached_answer_is_fast_and_reports_age(sb, monkeypatch):
    _three_day_fixture(sb)
    spawns = []
    monkeypatch.setattr(sb, "spawn_detached", lambda argv, **kw: spawns.append(argv) or object())
    fresh = sb.status_bar_payload(SID_A, sync=True)
    assert fresh["savings"]["session_tokens"] == 6500
    t0 = time.perf_counter()
    out = sb.status_bar_payload(SID_A)
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.3, f"cached path took {elapsed:.3f}s"
    assert out["savings"]["session_tokens"] == 6500
    assert out["savings_state"] == "fresh"
    assert 0 <= out["savings_age_s"] < 60
    assert out["refresh_started"] is False
    assert spawns == []


def test_stale_cache_answers_from_cache_and_starts_exactly_one_refresh(sb, monkeypatch):
    _three_day_fixture(sb)
    spawns = []
    monkeypatch.setattr(sb, "spawn_detached", lambda argv, **kw: spawns.append(argv) or object())
    sb.status_bar_payload(SID_A, sync=True)
    cache = sb._status_bar_cache_path(SID_A)
    data = json.loads(cache.read_text(encoding="utf-8"))
    data["computed_at"] -= 120
    cache.write_text(json.dumps(data), encoding="utf-8")

    first = sb.status_bar_payload(SID_A)
    second = sb.status_bar_payload(SID_A)
    assert first["savings"]["session_tokens"] == 6500
    assert first["savings_state"] == "stale"
    assert first["savings_age_s"] >= 120
    assert first["refresh_started"] is True
    assert second["refresh_started"] is False
    assert len(spawns) == 1
    argv = spawns[0]
    assert "status-bar" in argv and "--sync" in argv and SID_A in argv


def test_no_cache_starts_refresh_and_reports_loading(sb, monkeypatch):
    _three_day_fixture(sb)
    spawns = []
    monkeypatch.setattr(sb, "spawn_detached", lambda argv, **kw: spawns.append(argv) or object())
    out = sb.status_bar_payload(SID_A)
    assert out["savings"] is None
    assert out["savings_state"] == "loading"
    assert out["refresh_started"] is True
    assert len(spawns) == 1


def test_full_compute_within_5s_on_50k_rows(sb):
    base = datetime.now() - timedelta(days=29)
    rows = []
    for i in range(50_000):
        ts = base + timedelta(seconds=i * 50)
        sid = SID_A if i % 2 else SID_B
        rows.append((ts, "tool_archive", 100, 0.001, sid))
    _seed(sb, rows=rows)
    t0 = time.perf_counter()
    sav = sb._status_bar_compute_savings(SID_A)
    elapsed = time.perf_counter() - t0
    assert elapsed < 5.0, f"full compute took {elapsed:.2f}s"
    assert sav["session_tokens"] == 25_000 * 100


# --------------------------------------------------------------------------
# Refresh lock: atomic stale takeover and owner token (TR-22)
# --------------------------------------------------------------------------

def _stale_lock(sb, content="old-owner", age_s=200):
    lock = sb._status_bar_lock_path(SID_A)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(content, encoding="ascii")
    old = time.time() - age_s
    os.utime(lock, (old, old))
    return lock


def test_stale_lock_taken_over_by_one_caller_only(sb, monkeypatch):
    lock = _stale_lock(sb)
    spawns = []
    monkeypatch.setattr(sb, "spawn_detached", lambda argv, **kw: spawns.append(kw) or object())
    assert sb._status_bar_start_refresh(SID_A) is True
    assert sb._status_bar_start_refresh(SID_A) is False
    assert len(spawns) == 1
    assert lock.exists()
    assert not list(lock.parent.glob(lock.name + ".*.stale")), "quarantined lock left behind"


def test_racing_takeover_of_a_stale_lock_starts_one_child(sb, monkeypatch):
    """Both callers see the stale lock before either acts. The late one must
    not delete or replace the lock the first one just took."""
    import pathlib
    lock = _stale_lock(sb)
    spawns = []
    monkeypatch.setattr(sb, "spawn_detached", lambda argv, **kw: spawns.append(kw) or object())
    real_stat = pathlib.Path.stat
    raced = {"done": False}

    def racing_stat(self, *a, **kw):
        st = real_stat(self, *a, **kw)
        if not raced["done"] and self == lock:
            raced["done"] = True
            # Caller B runs to completion between A's staleness check and A's takeover.
            assert sb._status_bar_start_refresh(SID_A) is True
        return st

    monkeypatch.setattr(pathlib.Path, "stat", racing_stat)
    assert sb._status_bar_start_refresh(SID_A) is False
    monkeypatch.setattr(pathlib.Path, "stat", real_stat)
    assert len(spawns) == 1
    assert lock.read_text(encoding="ascii") == spawns[0]["env"][sb._STATUS_BAR_LOCK_TOKEN_ENV]
    assert not list(lock.parent.glob(lock.name + ".*.stale"))


def test_child_gets_the_lock_token_in_its_environment(sb, monkeypatch):
    spawns = []
    monkeypatch.setattr(sb, "spawn_detached", lambda argv, **kw: spawns.append((argv, kw)) or object())
    assert sb._status_bar_start_refresh(SID_A) is True
    argv, kw = spawns[0]
    token = kw["env"]["TO_STATUS_BAR_LOCK_TOKEN"]
    assert len(token) == 32
    assert sb._status_bar_lock_path(SID_A).read_text(encoding="ascii") == token
    assert not any("TOKEN" in a for a in argv), "the token travels by env, not the CLI"


def test_failed_spawn_releases_its_own_lock(sb, monkeypatch):
    monkeypatch.setattr(sb, "spawn_detached", lambda argv, **kw: None)
    assert sb._status_bar_start_refresh(SID_A) is False
    assert not sb._status_bar_lock_path(SID_A).exists()


def _run_release(sb, env_token):
    env = dict(os.environ)
    env.update(sb._sb_env)
    env.pop("TO_STATUS_BAR_LOCK_TOKEN", None)
    if env_token is not None:
        env["TO_STATUS_BAR_LOCK_TOKEN"] = env_token
    proc = subprocess.run(
        [sys.executable, str(MEASURE), "status-bar", "--session", SID_A,
         "--sync", "--release-lock"],
        capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return sb._status_bar_lock_path(SID_A)


@pytest.mark.parametrize("env_token", ["b" * 32, None, ""])
def test_child_keeps_a_lock_it_does_not_own(sb, env_token):
    _stale_lock(sb, content="a" * 32, age_s=0)
    lock = _run_release(sb, env_token)
    assert lock.exists()
    assert lock.read_text(encoding="ascii") == "a" * 32


def test_child_releases_its_own_lock(sb):
    _stale_lock(sb, content="a" * 32, age_s=0)
    lock = _run_release(sb, "a" * 32)
    assert not lock.exists()
