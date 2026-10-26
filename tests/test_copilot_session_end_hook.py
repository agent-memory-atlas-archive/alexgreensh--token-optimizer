"""Copilot has no lowercase `stop` event; end-of-session work runs on sessionEnd.

Run: python3 -m pytest tests/test_copilot_session_end_hook.py -v
"""
import importlib
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"


@pytest.fixture
def mods(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("TOKEN_OPTIMIZER_COPILOT_HOME", raising=False)
    monkeypatch.setenv("COPILOT_HOME", str(home / ".copilot"))
    py = tmp_path / "bin" / "python3"
    py.parent.mkdir(mode=0o755)
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    monkeypatch.setenv("TOKEN_OPTIMIZER_PYTHON", str(py))
    for name in ("copilot_install", "copilot_doctor", "copilot_hook_bridge"):
        sys.modules.pop(name, None)
    return (
        importlib.import_module("copilot_install"),
        importlib.import_module("copilot_doctor"),
        importlib.import_module("copilot_hook_bridge"),
        home / ".copilot",
    )


def _doctor_checks(doc):
    return {c["name"]: c for c in doc._hook_config_checks()}


def test_installer_wires_session_end_not_stop(mods):
    inst, _doc, _br, tmp = mods
    hooks = inst._hook_config(tmp / "bridge.py")["hooks"]
    assert "stop" not in hooks
    assert set(hooks) == {"sessionStart", "preToolUse", "postToolUse", "sessionEnd"}
    assert hooks["sessionEnd"][0]["bash"].endswith("bridge.py session-end") or "session-end" in hooks["sessionEnd"][0]["bash"]


def test_bridge_dispatches_session_end_and_legacy_stop(mods):
    _inst, _doc, br, _tmp = mods
    assert br._HANDLERS["session-end"] is br.handle_session_end
    assert br._HANDLERS["stop"] is br.handle_session_end


@pytest.mark.parametrize(
    "payload",
    [
        {"sessionId": "sess-1", "timestamp": 1, "cwd": "/x", "reason": "complete"},
        {"hook_event_name": "SessionEnd", "session_id": "sess-1", "reason": "user_exit"},
    ],
)
def test_session_end_removes_this_sessions_tally_only(mods, monkeypatch, payload):
    _inst, _doc, br, tmp = mods
    spawned = []
    monkeypatch.setattr(br, "spawn_detached", lambda *a, **k: spawned.append(a[0]) or object())
    to_dir = br._to_dir()
    mine = to_dir / "inflight-sess-1.json"
    other = to_dir / "inflight-sess-2.json"
    mine.write_text("{}")
    other.write_text("{}")
    br.handle_session_end(payload)
    assert not mine.exists()
    assert other.exists()
    kinds = [c[2] for c in spawned]
    assert kinds == ["copilot-rollup", "dashboard"]


def test_reinstall_replaces_legacy_stop_file(mods):
    inst, doc, _br, tmp = mods
    inst.install(home=tmp)
    path = tmp / "hooks" / "token-optimizer.json"
    data = json.loads(path.read_text())
    data["hooks"]["stop"] = data["hooks"].pop("sessionEnd")
    data["hooks"]["stop"][0]["bash"] = "X=1 py /p/copilot_hook_bridge.py stop"
    path.write_text(json.dumps(data))
    assert "TO session-end hook" in _doctor_checks(doc)
    assert _doctor_checks(doc)["TO session-end hook"]["status"] == "warn"
    inst.install(home=tmp)
    again = json.loads(path.read_text())["hooks"]
    assert "stop" not in again and "sessionEnd" in again
    assert "TO session-end hook" not in _doctor_checks(doc)


def test_doctor_ignores_foreign_stop_and_other_events(mods):
    inst, doc, _br, tmp = mods
    inst.install(home=tmp)
    path = tmp / "hooks" / "token-optimizer.json"
    data = json.loads(path.read_text())
    data["hooks"]["stop"] = [{"type": "command", "bash": "./my-own-script.sh"}]
    data["hooks"]["someFutureEvent"] = [{"type": "command", "bash": "x"}]
    path.write_text(json.dumps(data))
    checks = _doctor_checks(doc)
    assert checks["TO hook config"]["status"] == "ok"
    assert "TO session-end hook" not in checks


@pytest.mark.parametrize(
    "text",
    ["null", "[]", '"x"', "42", "true", "", "{not json", '{"hooks": []}', '{"hooks": "x"}', '{"hooks": false}', '{"hooks": 7}'],
)
def test_doctor_does_not_crash_on_non_object_json(mods, text):
    inst, doc, _br, tmp = mods
    inst.install(home=tmp)
    (tmp / "hooks" / "token-optimizer.json").write_text(text)
    check = _doctor_checks(doc)["TO hook config"]
    assert check["status"] == "fail"
    assert "copilot-install" in check["hint"]


def test_doctor_non_object_hooks_value_null_is_ok_shape(mods):
    inst, doc, _br, tmp = mods
    inst.install(home=tmp)
    (tmp / "hooks" / "token-optimizer.json").write_text('{"hooks": null}')
    assert _doctor_checks(doc)["TO hook config"]["status"] == "ok"


# --- installed-plugin behavior: run the generated hook command, not the source tree ---



def _installed_hook(inst, home):
    inst.install(home=home)
    data = json.loads((home / "hooks" / "token-optimizer.json").read_text())
    cmd = data["hooks"]["sessionEnd"][0]["bash"]
    # The fixture pins a stub interpreter so install() passes its trust gate; run
    # the otherwise unchanged generated command with a real one.
    parts = shlex.split(cmd)
    assert parts[1].endswith("python3")
    parts[1] = sys.executable
    return " ".join(shlex.quote(x) for x in parts), home / "token-optimizer"


def _run_session_end(cmd, home, payload):
    env = {**os.environ, "COPILOT_HOME": str(home), "HOME": str(home.parent)}
    return subprocess.run(["bash", "-c", cmd], input=json.dumps(payload), text=True,
                          capture_output=True, env=env, timeout=60)


def test_installer_writes_measure_locator_and_ships_no_measure_copy(mods):
    inst, _doc, _br, tmp = mods
    inst.install(home=tmp)
    plugin = tmp / "token-optimizer" / "plugin"
    assert not (plugin / "measure.py").exists()
    target = Path((plugin / "measure-path").read_text().strip())
    assert target.name == "measure.py" and target.is_file()


def test_installed_hook_cleans_up_and_spawns_refresh_via_locator(mods, tmp_path):
    inst, _doc, _br, tmp = mods
    cmd, to_dir = _installed_hook(inst, tmp)
    marker = tmp_path / "ran"
    stub = tmp_path / "stub" / "measure.py"
    stub.parent.mkdir()
    stub.write_text(
        "import sys, pathlib\n"
        f"d = pathlib.Path({str(marker)!r})\n"
        "d.mkdir(exist_ok=True)\n"
        "(d / sys.argv[1]).write_text(' '.join(sys.argv[1:]))\n"
    )
    (to_dir / "plugin" / "measure-path").write_text(f"{stub}\n")
    (to_dir / "inflight-sess-0009.json").write_text("{}")
    res = _run_session_end(cmd, tmp, {"sessionId": "sess-0009", "cwd": "/x", "timestamp": 1, "reason": "user_exit"})
    assert res.returncode == 0, res.stderr
    assert not (to_dir / "inflight-sess-0009.json").exists()
    deadline = time.time() + 15
    while time.time() < deadline and not ((marker / "copilot-rollup").exists() and (marker / "dashboard").exists()):
        time.sleep(0.2)
    # One marker file per spawned command, so the two detached children never
    # write the same file.
    lines = sorted(p.read_text() for p in marker.iterdir())
    assert lines == ["copilot-rollup --quiet", "dashboard --quiet"]


def test_installed_hook_cleans_up_even_when_measure_is_unreachable(mods):
    inst, doc, _br, tmp = mods
    cmd, to_dir = _installed_hook(inst, tmp)
    (to_dir / "plugin" / "measure-path").unlink()
    assert _doctor_checks(doc)["session-end refresh"]["status"] == "warn"
    (to_dir / "inflight-sess-0009.json").write_text("{}")
    res = _run_session_end(cmd, tmp, {"sessionId": "sess-0009", "reason": "complete"})
    assert res.returncode == 0, res.stderr
    assert not (to_dir / "inflight-sess-0009.json").exists()


def test_locator_pointing_at_non_measure_file_is_refused(mods, tmp_path):
    inst, _doc, br, tmp = mods
    inst.install(home=tmp)
    evil = tmp_path / "evil.py"
    evil.write_text("print(1)")
    plugin = tmp / "token-optimizer" / "plugin"
    (plugin / "measure-path").write_text(f"{evil}\n")
    # The bridge under test is the source-tree module, so point it at the
    # installed plugin dir to exercise the locator rule directly.
    br._MEASURE_LOCATOR = plugin / "measure-path"
    br._SCRIPT_DIR = plugin
    assert br._locate_measure_py() is None


def test_doctor_quiet_on_fresh_install(mods):
    inst, doc, _br, tmp = mods
    inst.install(home=tmp)
    assert "session-end refresh" not in _doctor_checks(doc)


# --- dashboard health aggregates the session-end checks ---

_DASH_PROBE = (
    "import json, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import measure\n"
    "print(json.dumps(measure._collect_copilot_hook_status_for_dashboard()['copilot_hooks']))\n"
)


def _dash(monkeypatch, tmp=None):
    """Run the dashboard collector in a child process with an isolated HOME.

    measure binds HOME-derived paths at import and stats the account's real home
    while doing so, so importing it in the test process would touch the real
    home outside any test. The child gets the fixture's throwaway home instead.
    """
    home = Path(os.environ["COPILOT_HOME"]).parent
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home),
           "TOKEN_OPTIMIZER_RUNTIME": "copilot"}
    res = subprocess.run([sys.executable, "-c", _DASH_PROBE, str(SCRIPTS)], env=env,
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr[-800:]
    return json.loads(res.stdout.strip().splitlines()[-1])


def test_dashboard_healthy_after_fresh_install(mods, monkeypatch):
    inst, _doc, _br, tmp = mods
    inst.install(home=tmp)
    hooks = _dash(monkeypatch)
    assert hooks["installed"] is True and hooks["partial"] is False


def test_dashboard_partial_for_dead_legacy_stop_wiring(mods, monkeypatch):
    inst, _doc, _br, tmp = mods
    inst.install(home=tmp)
    path = tmp / "hooks" / "token-optimizer.json"
    data = json.loads(path.read_text())
    data["hooks"]["stop"] = data["hooks"].pop("sessionEnd")
    data["hooks"]["stop"][0]["bash"] = "X=1 py /p/copilot_hook_bridge.py stop"
    path.write_text(json.dumps(data))
    hooks = _dash(monkeypatch)
    assert hooks["installed"] is True  # still installed, not hidden
    assert hooks["partial"] is True


def test_dashboard_partial_when_refresh_is_paused(mods, monkeypatch):
    inst, _doc, _br, tmp = mods
    inst.install(home=tmp)
    (tmp / "token-optimizer" / "plugin" / "measure-path").unlink()
    hooks = _dash(monkeypatch)
    assert hooks["installed"] is True and hooks["partial"] is True


def test_doctor_rejects_symlinked_sibling_measure_like_the_bridge(mods, tmp_path):
    inst, doc, _br, tmp = mods
    inst.install(home=tmp)
    plugin = tmp / "token-optimizer" / "plugin"
    (plugin / "measure-path").unlink()
    real = tmp_path / "real_measure.py"
    real.write_text("")
    (plugin / "measure.py").symlink_to(real)
    assert doc._measure_reachable(plugin) is False


# --- lock cleanup at session end: released lease is reclaimed, live holder is kept ---

_HOLDER = (
    "import sys, time\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from hook_runtime import lease_lock\n"
    "mode = sys.argv[3]\n"
    "with lease_lock(sys.argv[2], acquire_timeout=1, lease_seconds=float(sys.argv[4])) as got:\n"
    "    assert got\n"
    "    print('ready', flush=True)\n"
    "    if mode == 'hold':\n"
    "        sys.stdin.readline()\n"
)


def test_session_end_removes_a_released_lease_from_another_process(mods):
    inst, _doc, _br, tmp = mods
    cmd, to_dir = _installed_hook(inst, tmp)
    lock = to_dir / "inflight-sess-0009.lock"
    done = subprocess.run([sys.executable, "-c", _HOLDER, str(SCRIPTS), str(lock), "release", "30"],
                          capture_output=True, text=True, timeout=30)
    assert done.stdout.strip() == "ready", done.stderr
    assert lock.exists()  # a released lease leaves its tombstone behind
    res = _run_session_end(cmd, tmp, {"sessionId": "sess-0009", "reason": "complete"})
    assert res.returncode == 0, res.stderr
    assert not lock.exists()


def test_session_end_keeps_a_lease_a_live_process_still_holds(mods):
    inst, _doc, _br, tmp = mods
    cmd, to_dir = _installed_hook(inst, tmp)
    lock = to_dir / "inflight-sess-0009.lock"
    holder = subprocess.Popen([sys.executable, "-c", _HOLDER, str(SCRIPTS), str(lock), "hold", "30"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "ready"
        res = _run_session_end(cmd, tmp, {"sessionId": "sess-0009", "reason": "user_exit"})
        assert res.returncode == 0, res.stderr
        assert lock.exists()
    finally:
        holder.communicate("\n", timeout=30)


def test_session_end_removes_a_lease_released_long_ago(mods):
    """Past the post-release reuse window the lock is reclaimable even without
    the immediate-reclaim flag; the immediate case is the test above."""
    inst, _doc, _br, tmp = mods
    cmd, to_dir = _installed_hook(inst, tmp)
    lock = to_dir / "inflight-sess-0009.lock"
    done = subprocess.run([sys.executable, "-c", _HOLDER, str(SCRIPTS), str(lock), "release", "0.1"],
                          capture_output=True, text=True, timeout=30)
    assert done.stdout.strip() == "ready", done.stderr
    time.sleep(0.6)
    res = _run_session_end(cmd, tmp, {"sessionId": "sess-0009", "reason": "timeout"})
    assert res.returncode == 0, res.stderr
    assert not lock.exists()


def test_doctor_payload_check_fails_when_a_bridge_dependency_is_missing(mods):
    inst, doc, _br, tmp = mods
    inst.install(home=tmp)
    plugin = tmp / "token-optimizer" / "plugin"
    assert _doctor_checks(doc)["hook payload"]["status"] == "ok"
    (plugin / "runtime_env.py").unlink()
    check = _doctor_checks(doc)["hook payload"]
    assert check["status"] == "fail" and "runtime_env.py" in check["detail"]


def test_refresh_warning_does_not_promise_cleanup(mods):
    inst, doc, _br, tmp = mods
    inst.install(home=tmp)
    (tmp / "token-optimizer" / "plugin" / "measure-path").unlink()
    detail = _doctor_checks(doc)["session-end refresh"]["detail"]
    assert "cleanup" not in detail.lower()


def test_failed_rollup_does_not_stop_the_dashboard(mods, tmp_path):
    inst, _doc, _br, tmp = mods
    cmd, to_dir = _installed_hook(inst, tmp)
    out = tmp_path / "order2"
    out.mkdir()
    stub = tmp_path / "stub2" / "measure.py"
    stub.parent.mkdir()
    stub.write_text(
        "import sys, pathlib\n"
        f"d = pathlib.Path({str(out)!r})\n"
        "if sys.argv[1] == 'copilot-rollup':\n"
        "    raise SystemExit(3)\n"
        "(d / 'dashboard').write_text('ran')\n"
    )
    (to_dir / "plugin" / "measure-path").write_text(f"{stub}\n")
    res = _run_session_end(cmd, tmp, {"sessionId": "sess-0009", "reason": "complete"})
    assert res.returncode == 0, res.stderr
    deadline = time.time() + 15
    while time.time() < deadline and not (out / "dashboard").exists():
        time.sleep(0.1)
    assert (out / "dashboard").read_text() == "ran"


def _stub_measure(tmp_path, name, out):
    out.mkdir(exist_ok=True)
    stub = tmp_path / name / "measure.py"
    stub.parent.mkdir()
    stub.write_text(
        "import sys, pathlib\n"
        f"pathlib.Path({str(out)!r}, sys.argv[1]).write_text('ran')\n"
    )
    return stub


def _assert_cleanup_without_refresh(cmd, tmp, to_dir, out):
    (to_dir / "inflight-sess-0009.json").write_text("{}")
    res = _run_session_end(cmd, tmp, {"sessionId": "sess-0009", "reason": "complete"})
    assert res.returncode == 0, res.stderr
    assert not (to_dir / "inflight-sess-0009.json").exists()
    time.sleep(1.0)
    assert not any(out.iterdir())


def test_symlinked_locator_file_is_refused_but_cleanup_runs(mods, tmp_path):
    inst, doc, _br, tmp = mods
    cmd, to_dir = _installed_hook(inst, tmp)
    out = tmp_path / "out"
    stub = _stub_measure(tmp_path, "good", out)
    real = tmp_path / "real-locator"
    real.write_text(f"{stub}\n")
    locator = to_dir / "plugin" / "measure-path"
    locator.unlink()
    locator.symlink_to(real)
    assert _doctor_checks(doc)["session-end refresh"]["status"] == "warn"
    _assert_cleanup_without_refresh(cmd, tmp, to_dir, out)


def test_symlinked_measure_target_is_refused_but_cleanup_runs(mods, tmp_path):
    inst, doc, _br, tmp = mods
    cmd, to_dir = _installed_hook(inst, tmp)
    out = tmp_path / "out"
    stub = _stub_measure(tmp_path, "good", out)
    link = tmp_path / "link" / "measure.py"
    link.parent.mkdir()
    link.symlink_to(stub)
    (to_dir / "plugin" / "measure-path").write_text(f"{link}\n")
    assert _doctor_checks(doc)["session-end refresh"]["status"] == "warn"
    _assert_cleanup_without_refresh(cmd, tmp, to_dir, out)


def test_removed_checkout_target_still_cleans_up(mods, tmp_path):
    inst, doc, _br, tmp = mods
    cmd, to_dir = _installed_hook(inst, tmp)
    out = tmp_path / "out"
    stub = _stub_measure(tmp_path, "gone", out)
    (to_dir / "plugin" / "measure-path").write_text(f"{stub}\n")
    stub.unlink()
    assert _doctor_checks(doc)["session-end refresh"]["status"] == "warn"
    _assert_cleanup_without_refresh(cmd, tmp, to_dir, out)


@pytest.mark.parametrize("failure", ["oserror", "subprocess", "none"])
def test_spawn_failure_does_not_block_cleanup(mods, monkeypatch, failure):
    _inst, _doc, br, _tmp = mods
    if failure == "oserror":
        def boom(*a, **k):
            raise OSError("no spawn")
    elif failure == "subprocess":
        def boom(*a, **k):
            raise subprocess.SubprocessError("no spawn")
    else:
        def boom(*a, **k):
            return None
    monkeypatch.setattr(br, "spawn_detached", boom)
    to_dir = br._to_dir()
    tally = to_dir / "inflight-sess-0009.json"
    tally.write_text("{}")
    br.handle_session_end({"sessionId": "sess-0009", "reason": "complete"})
    assert not tally.exists()


def test_dashboard_partial_for_damaged_hook_payload(mods, monkeypatch):
    inst, _doc, _br, tmp = mods
    inst.install(home=tmp)
    (tmp / "token-optimizer" / "plugin" / "runtime_env.py").unlink()
    hooks = _dash(monkeypatch)
    assert hooks["installed"] is True  # the hook file is still there
    assert hooks["partial"] is True
