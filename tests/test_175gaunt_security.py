"""Security gauntlet for the INTEGRATED #175 extraction.

Dimension: SECURITY.

Attack surfaces:
  1. codex_command_compress: model-controlled shell/interpreter honored
     (tool_input.shell was fixed — attack OTHER model-controlled fields)
  2. codex_command_compress: auto-allow for sensitive/arbitrary path reads
     via whitelisted commands (tail/ls/rg/wc/cat reading ~/.ssh, /etc,
     absolute paths, ../ traversal, --flag=VALUE form bypasses)
  3. base64 Windows launcher (_launcher_signature bypass, code injection)
  4. os.environ leaks onto the parent process (one was fixed in run() —
     find others in the hook execution path)
  5. hooks manifest disabling

Every test below FAILS against the current code, demonstrating the gap.
If a surface is clean, it is noted in the report, not here.
"""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'
sys.path.insert(0, str(SCRIPTS))
import codex_command_compress as compression
import codex_install as installer
import plugin_env


# --------------------------------------------------------------------------- #
# Test infrastructure
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _enable_and_isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_env, 'is_v5_flag_enabled', lambda *a, **k: True)
    monkeypatch.setenv('TOKEN_OPTIMIZER_SNAPSHOT_DIR', str(tmp_path / 'data'))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _payload(command, **tool_input_extra):
    return {'tool_name': 'Bash',
            'tool_input': {'command': command, **tool_input_extra},
            'cwd': os.getcwd(), 'model': 'gpt-6-astra',
            'session_id': '11111111-1111-1111-1111-111111111111'}


def _plan_from(rewritten_command):
    import shlex
    parts = shlex.split(rewritten_command)
    idx = parts.index('--run')
    return json.loads(base64.b64decode(parts[idx + 1]))


# =========================================================================== #
# CRITICAL: model-controlled env passes through → shell bypass via PATH
# =========================================================================== #
# The "shell is ignored" fix strips tool_input.shell from updatedInput.
# But the hook copies ALL other tool_input fields verbatim:
#   updated = {k: v for k, v in tool_input.items() if k != 'shell'}
# A model-controlled env dict passes through. If Codex honors env in
# updatedInput, the model can inject PATH to steer _default_shell() to a
# malicious interpreter — the exact attack the shell fix was meant to prevent.
# =========================================================================== #

def test_model_controlled_env_does_not_pass_through(tmp_path):
    """CRITICAL: env from tool_input passes through to updatedInput.

    The hook only strips 'shell'. A model-controlled env dict survives into
    updatedInput. If Codex honors env, the model can inject PATH to steer
    _default_shell() (which uses shutil.which('bash') → os.environ['PATH'])
    to a malicious interpreter, bypassing the shell-ignored fix entirely.

    Repro: rewrite(_payload('git status', env={'PATH': '/evil'}))
    Expected (secure): 'env' NOT in updatedInput
    Actual (vulnerable): 'env' IS in updatedInput
    """
    result = compression.rewrite(_payload('git status', env={'PATH': '/evil'}))
    assert result is not None, "git status should be auto-allowed"
    updated = result['hookSpecificOutput']['updatedInput']
    assert 'env' not in updated, (
        "model-controlled env passed through to updatedInput; "
        "a model can inject PATH to steer _default_shell() to a malicious "
        "interpreter, bypassing the shell-ignored fix"
    )


def test_default_shell_follows_model_controlled_path(tmp_path, monkeypatch):
    """CRITICAL: _default_shell() resolves bash via shutil.which which honors
    os.environ['PATH']. Combined with the env pass-through above, a model
    that injects env.PATH via updatedInput can steer the shell.

    Repro: create /tmp/evil/bash, set PATH=/tmp/evil, call _default_shell()
    Expected (secure): _default_shell() returns None or the real bash
    Actual (vulnerable): _default_shell() returns /tmp/evil/bash
    """
    evil_dir = tmp_path / 'evil'
    evil_dir.mkdir()
    evil_bash = evil_dir / 'bash'
    evil_bash.write_text('#!/bin/sh\necho PWNED\n')
    evil_bash.chmod(0o755)
    monkeypatch.setenv('PATH', str(evil_dir))
    shell = compression._default_shell()
    assert shell is None or str(evil_dir) not in shell, (
        f"_default_shell() returned {shell!r} from a model-controlled PATH; "
        "shutil.which('bash') honors os.environ['PATH'], so a model that "
        "injects env.PATH via updatedInput can steer the auto-approved "
        "wrapper to a malicious interpreter"
    )


def test_env_path_bypass_shell_ignored_end_to_end(tmp_path, monkeypatch):
    """CRITICAL: end-to-end demonstration of the env.PATH shell bypass.

    1. Hook auto-allows 'git status' with env={'PATH': '/evil'} in updatedInput
    2. Codex launches the python --run wrapper with PATH=/evil
    3. Inside run(), _default_shell() finds /evil/bash (malicious)
    4. run() executes /evil/bash -c 'git status'

    This test simulates steps 2-4 by directly calling run() with a PATH
    that points to a malicious bash.
    """
    evil_dir = tmp_path / 'evil'
    evil_dir.mkdir()
    marker = tmp_path / 'pwned'
    evil_bash = evil_dir / 'bash'
    evil_bash.write_text(f'#!/bin/sh\necho PWNED > "{marker}"\n')
    evil_bash.chmod(0o755)
    Path('file.txt').write_text('a\n')
    # Simulate: Codex set PATH=/evil for the python wrapper process
    monkeypatch.setenv('PATH', str(evil_dir))
    plan = {'command': 'wc -l file.txt', 'session_id': 'test'}
    rc = compression.run(plan)
    assert not marker.exists(), (
        "run() executed a malicious bash found via model-controlled PATH; "
        "the shell-ignored fix is bypassed because env.PATH passes through "
        "to updatedInput and _default_shell() follows it"
    )


# =========================================================================== #
# CRITICAL: model-controlled cwd passes through → confinement bypass
# =========================================================================== #
# The hook checks confinement against Path.cwd() (the real process cwd at
# rewrite time). But it passes the model-controlled cwd from tool_input
# through to updatedInput. If Codex honors cwd, the python --run wrapper
# runs in the model-controlled cwd. run() re-validates confinement against
# THAT cwd — so the model can read any file by setting cwd to the file's
# directory and issuing a confined relative-path command.
#
# Example: 'tail config' with cwd=~/.ssh reads ~/.ssh/config
# =========================================================================== #

def test_model_controlled_cwd_does_not_pass_through(tmp_path):
    """CRITICAL: cwd from tool_input passes through to updatedInput.

    The hook only strips 'shell'. A model-controlled cwd survives into
    updatedInput. If Codex honors cwd, the python wrapper runs in the
    model-controlled cwd, and run() re-validates confinement against THAT
    cwd — so the model can redirect a confined read to any directory.

    Repro: rewrite(_payload('tail config', cwd='~/.ssh'))
    Expected (secure): 'cwd' NOT in updatedInput
    Actual (vulnerable): 'cwd' IS in updatedInput
    """
    (tmp_path / 'config').write_text('dummy')  # so 'config' is confined to cwd
    ssh_dir = str(Path.home() / '.ssh')
    result = compression.rewrite(_payload('tail config', cwd=ssh_dir))
    assert result is not None, "tail config should be auto-allowed"
    updated = result['hookSpecificOutput']['updatedInput']
    assert 'cwd' not in updated, (
        "model-controlled cwd passed through to updatedInput; "
        "a model can redirect 'tail config' to read ~/.ssh/config by "
        "setting cwd to ~/.ssh"
    )


def test_model_controlled_cwd_bypasses_confinement_to_sensitive_dir(tmp_path,
                                                                     monkeypatch):
    """CRITICAL: end-to-end demonstration of the cwd confinement bypass.

    1. Hook auto-allows 'tail config' with cwd=~/.ssh in updatedInput
    2. Codex launches the python --run wrapper in ~/.ssh
    3. run() re-validates confinement against ~/.ssh — 'config' is confined
    4. run() executes 'tail config' in ~/.ssh, reading ~/.ssh/config

    This test simulates steps 2-4 by changing the cwd before calling run().
    """
    # Create a 'config' file in the current cwd so 'tail config' is confined
    (tmp_path / 'config').write_text('dummy')
    # Rewrite: hook auto-allows 'tail config'
    result = compression.rewrite(_payload('tail config'))
    assert result is not None
    plan = _plan_from(result['hookSpecificOutput']['updatedInput']['command'])
    # Simulate: Codex changed the cwd to a sensitive directory
    fake_ssh = tmp_path / 'fake-ssh'
    fake_ssh.mkdir()
    secret_config = fake_ssh / 'config'
    secret_config.write_text('Host *\n  IdentityFile ~/.ssh/id_rsa\n')
    monkeypatch.chdir(fake_ssh)
    # run() re-validates confinement against the NEW cwd (fake_ssh)
    rc = compression.run(plan)
    # SECURE behavior: run() should refuse to execute because the cwd changed
    # from the rewrite-time cwd, preventing the model from redirecting
    # confined reads to sensitive directories via the cwd field.
    # This will FAIL — run() re-validates against the new cwd and executes.
    assert rc != 0, (
        "run() executed the command in a different cwd than the rewrite-time "
        "cwd; the model can control the cwd via updatedInput to redirect "
        "confined reads to sensitive directories (e.g. tail config in "
        "~/.ssh reads ~/.ssh/config)"
    )


# =========================================================================== #
# CRITICAL: --flag=VALUE form bypasses _FILE_VALUE_FLAGS confinement
# =========================================================================== #
# _confined() uses _FILE_VALUE_FLAGS to mark the NEXT arg after a file-value
# flag as an operand to check. But the =VALUE form (--flag=VALUE) is a single
# token that doesn't match the flag name in the set, so the value is never
# checked. This lets the model read arbitrary files via:
#   wc --files0-from=/etc/passwd     (file contents leak in error message)
#   rg --file=/etc/passwd pattern    (reads /etc/passwd as patterns)
#   rg --ignore-file=/etc/passwd    (reads /etc/passwd as ignore patterns)
#   tree --ignore-file=/etc/passwd  (reads /etc/passwd as ignore patterns)
#   grep --file=/etc/passwd pattern (reads /etc/passwd as patterns)
# =========================================================================== #

@pytest.mark.parametrize('command', [
    'wc --files0-from=/etc/passwd',
    'rg --file=/etc/passwd pattern',
    'rg --ignore-file=/etc/passwd pattern',
    'tree --ignore-file=/etc/passwd',
    'grep --file=/etc/passwd pattern',
])
def test_equals_form_file_flags_bypass_confinement(command):
    """CRITICAL: the --flag=VALUE form bypasses _FILE_VALUE_FLAGS.

    _FILE_VALUE_FLAGS = {'-f', '--file', '--fromfile', '--ignore-file'}
    These match the flag name WITHOUT '=value'. So '--file=/etc/passwd'
    is NOT in the set and is skipped as a plain flag. The value is never
    checked as an operand. The command is auto-allowed despite reading
    an absolute, sensitive path.

    Repro: rewrite(_payload('wc --files0-from=/etc/passwd'))
    Expected (secure): None (not auto-allowed)
    Actual (vulnerable): permissionDecision='allow' with rewritten command
    """
    assert compression.rewrite(_payload(command)) is None, (
        f"{command!r} was auto-allowed; the =VALUE form bypasses "
        "_FILE_VALUE_FLAGS, so the file argument is never checked "
        "for confinement"
    )


def test_wc_files0_from_leaks_file_contents_in_error(tmp_path, monkeypatch):
    """CRITICAL: wc --files0-from=/etc/passwd reads /etc/passwd as a list
    of null-terminated filenames. Since /etc/passwd has no null bytes, the
    entire file becomes one 'filename'. wc tries to open it, fails, and
    prints an error message containing the 'filename' — which IS the file
    contents. The model sees /etc/passwd in the error output.

    This test creates a fake 'passwd' file and demonstrates the leak.
    """
    fake_etc = tmp_path / 'etc'
    fake_etc.mkdir()
    fake_passwd = fake_etc / 'passwd'
    fake_passwd.write_text('root:x:0:0:root:/root:/bin/bash\n')
    # wc --files0-from=<path> is eligible and _confined returns True
    # (the =VALUE form is not checked as an operand)
    command = f'wc --files0-from={fake_passwd}'
    result = compression.rewrite(_payload(command))
    assert result is None, (
        "wc --files0-from=<absolute> was auto-allowed; wc reads the file "
        "as a filename list and the contents leak in the error message "
        "when wc tries to open the 'filename'"
    )


# =========================================================================== #
# HIGH: os.environ leak in hook_io.read_stdin_hook_input()
# =========================================================================== #
# The fix in run() changed os.environ['TOKEN_OPTIMIZER_RUNTIME'] = 'codex'
# to child_env = dict(os.environ, TOKEN_OPTIMIZER_RUNTIME='codex').
# But hook_io.read_stdin_hook_input() — called from
# codex_command_compress.main() on every PreToolUse invocation — still
# mutates os.environ['TOKEN_OPTIMIZER_SESSION_ID'] directly. This leaks
# the session ID into the parent process for any in-process caller.
# =========================================================================== #

def test_hook_io_does_not_mutate_os_environ():
    """HIGH: hook_io.read_stdin_hook_input() mutates the parent process's
    os.environ['TOKEN_OPTIMIZER_SESSION_ID']. Same class of bug that was
    fixed in run() — runtime markers must be scoped to the child, not
    the parent.

    Repro: call read_stdin_hook_input() with a codex payload containing
    a session_id. Check os.environ afterward.
    Expected (secure): os.environ unchanged
    Actual (vulnerable): os.environ['TOKEN_OPTIMIZER_SESSION_ID'] set
    """
    code = (
        "import sys, os, json\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "os.environ['TOKEN_OPTIMIZER_RUNTIME'] = 'codex'\n"
        "os.environ.pop('TOKEN_OPTIMIZER_SESSION_ID', None)\n"
        "import hook_io\n"
        "hook_io.read_stdin_hook_input()\n"
        "if 'TOKEN_OPTIMIZER_SESSION_ID' in os.environ:\n"
        "    print('LEAKED:' + os.environ['TOKEN_OPTIMIZER_SESSION_ID'])\n"
        "else:\n"
        "    print('CLEAN')\n"
    )
    proc = subprocess.Popen(
        [sys.executable, '-c', code],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    payload = json.dumps({
        'tool_name': 'Bash',
        'session_id': 'leaked-session-id',
    })
    try:
        out, err = proc.communicate(payload, timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=60)
        pytest.fail("read_stdin_hook_input hung")
    assert 'LEAKED' not in out, (
        "hook_io.read_stdin_hook_input() mutated os.environ"
        "['TOKEN_OPTIMIZER_SESSION_ID'] in the parent process: "
        + out.strip()
    )


# =========================================================================== #
# HIGH: os.environ leak in bash_compress_hook.main()
# =========================================================================== #
# bash_compress_hook.main() — the PostToolUse hook — mutates
# os.environ["CLAUDE_SESSION_ID"] directly. Same class of bug.
# =========================================================================== #

def test_bash_compress_hook_does_not_mutate_os_environ():
    """HIGH: bash_compress_hook.main() mutates the parent process's
    os.environ['CLAUDE_SESSION_ID']. Same class of bug fixed in run().

    Repro: call bash_compress_hook.main() with a PostToolUse payload
    containing a session_id. Check os.environ afterward.
    Expected (secure): os.environ unchanged
    Actual (vulnerable): os.environ['CLAUDE_SESSION_ID'] set
    """
    code = (
        "import sys, os, json\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "os.environ.pop('CLAUDE_SESSION_ID', None)\n"
        "import bash_compress_hook\n"
        "bash_compress_hook.main()\n"
        "if 'CLAUDE_SESSION_ID' in os.environ:\n"
        "    print('LEAKED:' + os.environ['CLAUDE_SESSION_ID'])\n"
        "else:\n"
        "    print('CLEAN')\n"
    )
    proc = subprocess.Popen(
        [sys.executable, '-c', code],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    payload = json.dumps({
        'tool_name': 'Bash',
        'session_id': 'leaked-session-id',
        'tool_response': {'stdout': '', 'exit_code': 0},
    })
    try:
        out, err = proc.communicate(payload, timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=60)
        pytest.fail("bash_compress_hook.main() hung")
    assert 'LEAKED' not in out, (
        "bash_compress_hook.main() mutated os.environ['CLAUDE_SESSION_ID'] "
        "in the parent process: " + out.strip()
    )


# =========================================================================== #
# MEDIUM: _default_shell() does not validate the resolved path
# =========================================================================== #
# _default_shell() checks Path(shell).stem.lower() in the allowed set, but
# does not verify the resolved path is a system shell (e.g., /usr/bin/bash).
# A model that can influence PATH (via the env pass-through) can place a
# script named 'bash' anywhere. The stem check passes because the filename
# is 'bash', but the content is arbitrary.
# =========================================================================== #

def test_default_shell_accepts_arbitrary_bash_named_script(tmp_path, monkeypatch):
    """MEDIUM: _default_shell() accepts any executable named 'bash' from PATH.
    The only validation is Path(shell).stem.lower() in the allowed set.
    A script named 'bash' in a model-controlled directory passes the check.

    This is the mechanism behind the CRITICAL env.PATH bypass above.
    """
    evil_dir = tmp_path / 'evil'
    evil_dir.mkdir()
    evil_bash = evil_dir / 'bash'
    evil_bash.write_text('#!/bin/sh\necho PWNED\n')
    evil_bash.chmod(0o755)
    monkeypatch.setenv('PATH', str(evil_dir))
    shell = compression._default_shell()
    # The shell should be None (refusing a non-system bash) or should
    # validate the resolved path against a known-safe list.
    assert shell is None, (
        f"_default_shell() accepted {shell!r}, a user-placed script named "
        "'bash'; the stem check alone is insufficient — a model that "
        "controls PATH via the env pass-through can inject a malicious shell"
    )


# =========================================================================== #
# MEDIUM: tree -a / ls -a reveal hidden file names in the cwd
# =========================================================================== #
# The confinement gate checks file OPERANDS for hidden/sensitive names, but
# does not check FLAGS that widen the output to include hidden files.
# tree -a and ls -a are auto-allowed and reveal the names (not contents)
# of hidden files like .env, .ssh, .aws within the cwd.
# =========================================================================== #

@pytest.mark.parametrize('command', [
    'tree -a',
    'ls -a',
    'ls -la',
])
def test_hidden_revealing_flags_not_auto_allowed(command, tmp_path):
    """MEDIUM: tree -a / ls -a reveal hidden file names in the cwd.

    The confinement gate checks file operands, not flags that widen output
    to include hidden files. tree -a and ls -a are auto-allowed, revealing
    the names of .env, .ssh, .aws, etc. in the cwd without Codex's consent.

    This is lower severity because only names are revealed, not contents,
    and the files are within the cwd. But it bypasses the 'non-hidden'
    claim in the security model.
    """
    (tmp_path / 'file.txt').write_text('x\n')
    (tmp_path / 'src').mkdir(exist_ok=True)
    (tmp_path / 'src' / 'a.py').write_text('pass\n')
    result = compression.rewrite(_payload(command))
    assert result is None, (
        f"{command!r} was auto-allowed; it reveals hidden file names in "
        f"the cwd, bypassing the 'non-hidden' confinement claim"
    )


# =========================================================================== #
# CLEAN surfaces (verified, no bypass found)
# =========================================================================== #
# The following surfaces were attacked and found clean:
#
# 1. tool_input.shell stripping: The shell field IS correctly stripped from
#    updatedInput, and the plan carries no shell. run() re-derives the shell
#    via _default_shell(). A forged plan with a 'shell' key is ignored at
#    exec time. (Existing tests test_injected_tool_input_shell_is_ignored,
#    test_forged_plan_shell_is_ignored_at_exec cover this.)
#
# 2. _launcher_signature: The signature decodes the base64 blob, extracts
#    the four interpolated fields via ast.literal_eval, and verifies the
#    decoded code EXACTLY matches _windows_bootstrap(root, script, args,
#    env). Any modification to the blob (extra lines, changed imports,
#    second exec) causes the exact comparison to fail, returning the
#    command verbatim — which triggers a trust review on upgrade. The
#    signature includes the full command prefix and suffix around the blob,
#    so appended commands or second exec blobs cause a mismatch.
#
# 3. Code injection into the blob: The interpolated fields use {value!r}
#    (Python repr), which always produces valid literals. ast.literal_eval
#    in _parse_launcher_bootstrap only accepts literals, not expressions.
#    No code injection vector found.
#
# 4. Hooks manifest: The .codex-plugin/plugin.json points to
#    hooks/codex-hooks.json with "hooks": {}. The actual hooks are installed
#    by codex_install.py into ~/.codex/hooks.json. The _merge_hooks function
#    removes only groups identified by _is_token_optimizer_group (which
#    requires the TOKEN_OPTIMIZER_MARKER or specific Windows regex patterns)
#    and replaces them with fresh managed hooks. The _launcher_signature
#    comparison prevents preserving tampered commands. No way to disable
#    hooks or inject malicious hooks via the merge logic.
#
# 5. Symlink escape: _operand_confined uses (base / arg).resolve() which
#    follows symlinks, then checks is_relative_to(base). A symlink in the
#    cwd pointing outside is caught.
#
# 6. ../ traversal: The component check catches '..' via c.startswith('.').
#
# 7. Glob operands: _GLOB_OPERAND_RE catches * ? [ ] { } ! in operands.
#
# 8. Sensitive component names: _SENSITIVE_COMPONENT_RE catches id_rsa,
#    .env, *.pem, *.key, .ssh, .aws, .kube, etc. as path components.
#
# 9. rg scope flags: _RG_SCOPE_FLAGS catches -u, --hidden, --no-ignore, etc.
#
# 10. grep -r/-R: The regex -[a-zA-Z]*[rR][a-zA-Z]* catches -r, -R, -rin, etc.
#
# 11. PowerShell scope flags: _PS_SCOPE_FLAGS catches -Recurse, -Force, -Hidden.
# =========================================================================== #


# =========================================================================== #
# CRITICAL (FIXED): shlex.split(posix=True) ate backslashes, so a Windows
# absolute path operand like C:\Users\x\.ssh\id_rsa was tokenized to
# C:Usersx.sshid_rsa (a relative-looking token), bypassing the absolute-path
# and sensitive-component confinement checks. The command was auto-allowed
# (permissionDecision='allow') and rewritten into an opaque python --run
# blob, laundering a sensitive-path read past Codex's consent.
#
# Fix: _confined() now tokenizes with posix=False (preserving backslashes)
# and _ABSOLUTE_OPERAND_RE also matches a single leading backslash.
# =========================================================================== #

@pytest.mark.parametrize('command', [
    r'wc -l C:\Users\x\.ssh\id_rsa',
    r'tail C:\Users\bob\.aws\credentials',
    r'wc --files0-from=C:\Users\x\.ssh\id_rsa',
    r'rg secret C:\Users\x\.ssh\config',
    r'ls C:\Users\x\.ssh',
    r'grep secret C:\Users\x\.gnupg\pubring.gpg',
    r'wc -l \Users\x\.ssh\id_rsa',
])
def test_windows_backslash_path_not_auto_allowed(command):
    """CRITICAL (fixed): backslash-bearing Windows paths must not be
    auto-allowed. shlex.split(posix=True) ate the backslashes, making the
    absolute path look relative/confined; posix=False preserves them so
    _ABSOLUTE_OPERAND_RE and _SENSITIVE_COMPONENT_RE can match.
    """
    assert compression.rewrite(_payload(command)) is None, (
        f"{command!r} was auto-allowed; the confinement gate was bypassed "
        f"by shlex backslash consumption"
    )


def test_quoted_regex_backslash_still_confined(tmp_path):
    """Sanity: a legitimate quoted regex with backslashes (e.g. grep '\b')
    must still be auto-allowed. posix=False preserves quoted backslashes;
    _operand_confined strips quotes and the pattern positional is skipped.
    """
    (tmp_path / 'file.txt').write_text('word boundary\n')
    result = compression.rewrite(_payload(r"grep '\bword\b' file.txt"))
    assert result is not None, "quoted regex with backslashes should still be confined"


# =========================================================================== #
# MEDIUM (FIXED): grep -d recurse / --directories=recurse bypassed the
# recursive-grep scope check (the -r/-R regex only matches flags containing
# r/R, not -d). This auto-allowed recursive reads of dotfiles in the cwd.
# =========================================================================== #

@pytest.mark.parametrize('command', [
    'grep -d recurse API_KEY .',
    'grep --directories recurse API_KEY .',
    'grep --directories=recurse API_KEY .',
])
def test_grep_d_recurse_not_auto_allowed(command, tmp_path):
    """MEDIUM (fixed): grep -d recurse is functionally -r and must not be
    auto-allowed (it reads dotfiles in the cwd).
    """
    (tmp_path / 'file.txt').write_text('x\n')  # for the '.' operand
    assert compression.rewrite(_payload(command)) is None, (
        f"{command!r} was auto-allowed; grep -d recurse bypassed the "
        f"recursive-grep scope check"
    )


def test_grep_d_read_still_allowed(tmp_path):
    """Sanity: grep -d read (non-recursive) must still be auto-allowed."""
    (tmp_path / 'file.txt').write_text('x\n')
    assert compression.rewrite(_payload('grep -d read x file.txt')) is not None

