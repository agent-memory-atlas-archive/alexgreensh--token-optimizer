"""Codex command rewrite adapter. Execute once, retain failures and archive originals.

Security model (hardened relative to the original design):

- ``tool_input.shell`` is IGNORED, never propagated. A prompt-injected model
  could otherwise point this auto-approved wrapper at an attacker-named
  interpreter. Both the rewrite quoting and the ``--run`` exec always use the
  runtime-default shell resolved here, and the injected field is stripped from
  the ``updatedInput`` we hand back so it cannot survive into the tool call.
- ``permissionDecision: 'allow'`` is emitted ONLY for commands whose file
  operands are provably confined to the working directory (non-hidden,
  non-sensitive, non-absolute, glob-free, no symlink escape). Eligible but
  non-confined commands are passed through UNREWRITTEN -- no updatedInput and
  no decision -- so Codex's own consent evaluates the literal command text.
  Rewriting an unvetted read would launder e.g. ``head ~/.ssh/id_rsa`` into an
  opaque ``python ... --run <blob>`` that path-based rules cannot see; under a
  permissive ``python`` policy that would execute silently. Pass-through keeps
  Codex's consent fully informed regardless of whether it checks the original
  or the rewritten command.
- ``--run`` revalidates eligibility AND confinement at exec time and re-derives
  the shell, so a forged plan can never widen what this wrapper executes.
"""
import base64
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid

from bash_whitelist import has_dangerous_chars, is_whitelisted

MAX_COMPRESS_BYTES = 8 * 1024 * 1024

# --- Confinement gate for the 'allow' decision + rewrite ----------------------
# A command is wrapped (and auto-allowed) only when every file operand stays
# provably inside the working directory and names nothing sensitive. Failing
# the gate costs only the compression: the command reaches Codex verbatim.
_ABSOLUTE_OPERAND_RE = re.compile(r'^(?:/|\\\\|[A-Za-z]:[\\/])')
_GLOB_OPERAND_RE = re.compile(r'[*?\[\]{}!]')
# Path components that hold credential/key material or secret stores. Applied
# per-component so `src/.ssh/config` and `.env.local` are caught, not just
# top-level names.
_SENSITIVE_COMPONENT_RE = re.compile(
    r'(?i)(?:^id_(?:rsa|dsa|ecdsa|ed25519)$|^\.env(?:\..*)?$|.*\.(?:pem|key|p12|'
    r'pfx|jks|keystore|kdbx?|ppk|asc|gpg)$|^\.netrc$|^\.pgpass$|^\.npmrc$|'
    r'^\.pypirc$|^credentials(?:\.[a-z]+)?$|^secrets?\.(?:json|ya?ml|toml|env|txt)$|'
    r'^\.ssh$|^\.gnupg$|^\.aws$|^\.azure$|^\.kube$|^\.docker$|^\.config$)'
)
# Flags that widen a search's read scope into hidden/ignored files. `rg` is
# recursive-by-default but skips hidden+ignored files unless one of these is
# given; `grep` only recurses (and thereby reads in-tree dotfiles) with -r/-R.
_RG_SCOPE_FLAGS = frozenset({
    '-u', '-uu', '-uuu', '--hidden', '--no-ignore', '--no-ignore-vcs',
    '--no-ignore-dot', '--no-ignore-parent', '--files', '--no-require-git',
})
# Flags whose following argument is itself a file operand (`rg -f pats`,
# `tree --fromfile f`). The value must pass the path check like any operand,
# and a pattern-supplying flag (-e/-f) means there is no pattern positional.
_PATTERN_FLAGS = frozenset({'-e', '--regexp', '-f', '--file', '--fromfile'})
_FILE_VALUE_FLAGS = frozenset({'-f', '--file', '--fromfile', '--ignore-file'})
# First positional of these commands is a pattern, not a path.
_PATTERN_FIRST_COMMANDS = frozenset({'rg', 'grep'})
# PowerShell switches that widen a literal Get-Content/Get-ChildItem into
# recursion or hidden-file reads.
_PS_SCOPE_FLAGS = frozenset({'-recurse', '-force', '-hidden'})


def eligible(command):
    if not isinstance(command, str) or not command.strip() or len(command) > 16000:
        return False
    if has_dangerous_chars(command) or 'codex_command_compress' in command:
        return False
    # Literal read-only PowerShell cmdlets. No script blocks, expressions or
    # profile functions; the original text is passed intact to the same shell.
    if re.match(r'^(Get-Content|Get-ChildItem)\s', command, re.I):
        return not re.search(r'[{}()]|\s-(?:Wait|Stream)\b', command, re.I)
    try:
        args = shlex.split(command)
    except ValueError:
        return False
    if not args:
        return False
    # The shared Claude list also contains builds, tests and write-capable
    # subcommands. Codex 'allow' must only rewrite inspection commands.
    if args[0] == 'git':
        return len(args) > 1 and args[1] in ('status', 'log') and not any(
            a.startswith(('--output', '--ext-diff', '--textconv')) for a in args[2:])
    if args[0] in ('rg', 'grep', 'ls', 'tree', 'wc', 'head', 'tail'):
        return not any(a.startswith(('--pre', '--hostname-bin')) for a in args[1:]) and is_whitelisted(command)
    return False


def _default_shell():
    """The shell this runtime would use. ``tool_input.shell`` is never read:
    it is model-controlled text and must not steer an auto-approved hook."""
    if os.name == 'nt':
        shell = shutil.which('pwsh') or shutil.which('powershell')
    else:
        shell = shutil.which('bash') or shutil.which('sh')
    if not shell or Path(shell).stem.lower() not in ('pwsh', 'powershell', 'bash', 'sh', 'zsh'):
        return None
    return shell


def _operand_confined(arg, base):
    """True when a single operand is a provably in-cwd, non-hidden,
    non-sensitive path -- or not path-shaped at all."""
    arg = arg.strip('"\'')  # posix=False shlex keeps quote characters
    if (not arg or arg.startswith(('~', '$')) or _ABSOLUTE_OPERAND_RE.match(arg)
            or _GLOB_OPERAND_RE.search(arg)):
        return False
    components = [c for c in re.split(r'[\\/]+', arg) if c and c != '.']
    if any(c.startswith('.') or _SENSITIVE_COMPONENT_RE.search(c) for c in components):
        return False
    try:
        return (base / arg).resolve().is_relative_to(base)
    except OSError:
        return False


def _confined(command, base):
    """True when every file operand of `command` is confined to `base`.

    `base` is always the real process cwd at call time -- never the
    model-influenced payload cwd, which cannot be trusted as a confinement
    boundary.
    """
    if re.match(r'^(Get-Content|Get-ChildItem)\s', command, re.I):
        # PowerShell quoting differs from POSIX; flag-looking tokens are still
        # dashed and the rest are operands.
        try:
            args = shlex.split(command, posix=False)[1:]
        except ValueError:
            return False
        if any(a.strip('"\'').lower() in _PS_SCOPE_FLAGS for a in args):
            return False
        cmd0 = ''
    else:
        try:
            args = shlex.split(command)
        except ValueError:
            return False
        cmd0 = args[0] if args else ''
        args = args[1:]
    if cmd0 == 'rg' and any(a in _RG_SCOPE_FLAGS for a in args):
        return False
    if cmd0 == 'grep' and any(
            a == '--recursive' or a == '--dereference-recursive'
            or re.fullmatch(r'-[a-zA-Z]*[rR][a-zA-Z]*', a) for a in args):
        return False
    expect_pattern = cmd0 in _PATTERN_FIRST_COMMANDS
    file_value_next = False
    for arg in args:
        if file_value_next:
            file_value_next = False
        elif arg.startswith('-') and arg != '-':
            if arg in _FILE_VALUE_FLAGS:
                file_value_next = True
            if arg in _PATTERN_FLAGS:
                expect_pattern = False
            continue
        elif expect_pattern:
            expect_pattern = False
            continue
        if not _operand_confined(arg, base):
            return False
    return True


def rewrite(payload):
    from plugin_env import is_v5_flag_enabled
    if not is_v5_flag_enabled('v5_bash_compress', 'TOKEN_OPTIMIZER_BASH_COMPRESS', default=True):
        return None
    if payload.get('tool_name') != 'Bash':
        return None
    tool_input = payload.get('tool_input') or {}
    command = tool_input.get('command')
    shell = _default_shell()
    if not eligible(command) or not shell:
        return None
    if not _confined(command, Path.cwd().resolve()):
        # Provably-safe reads only. Anything else passes through untouched so
        # Codex's consent sees the literal command (e.g. `head ~/.ssh/id_rsa`
        # prompts exactly as it would without this hook).
        return None
    plan = {'command': command, 'session_id': payload.get('session_id'),
            'model': payload.get('model')}
    encoded = base64.b64encode(json.dumps(plan).encode()).decode()
    argv = [sys.executable, str(Path(__file__).resolve()), '--run', encoded]
    if Path(shell).stem.lower() in ('pwsh', 'powershell'):
        rewritten = '& ' + ' '.join("'" + a.replace("'", "''") + "'" for a in argv) + '; exit $LASTEXITCODE'
    else:
        rewritten = shlex.join(argv)
    updated = {k: v for k, v in tool_input.items() if k != 'shell'}
    updated['command'] = rewritten
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'allow',
                                  'updatedInput': updated}}


def run(plan):
    command = plan.get('command') if isinstance(plan, dict) else None
    # Revalidate at execution time: never turn a trusted wrapper into a generic
    # command launcher. Ineligible or non-confined commands are not executed by
    # this wrapper -- a forged plan cannot exceed the inspection envelope.
    if not eligible(command) or not _confined(command, Path.cwd().resolve()):
        print('Token Optimizer: command is not eligible', file=sys.stderr)
        return 2
    shell = _default_shell()
    if not shell:
        print('Token Optimizer: no usable default shell', file=sys.stderr)
        return 2
    os.environ['TOKEN_OPTIMIZER_RUNTIME'] = 'codex'
    if plan.get('session_id'):
        os.environ['TOKEN_OPTIMIZER_SESSION_ID'] = str(plan['session_id'])
    if Path(shell).stem.lower() in ('pwsh', 'powershell'):
        tail = '; $toSucceeded=$?; $toExit=$LASTEXITCODE; if ($null -ne $toExit) { exit $toExit }; if (-not $toSucceeded) { exit 1 }'
        argv = [shell, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', command + tail]
    else:
        argv = [shell, '-c', command]
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        result = subprocess.run(argv, stdout=output, stderr=errors,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        output.seek(0, 2)
        size = output.tell()
        output.seek(0)
        errors.seek(0)
        # Failure/oversized output is streamed verbatim, never buffered in RAM.
        if result.returncode != 0 or size > MAX_COMPRESS_BYTES:
            shutil.copyfileobj(output, sys.stdout.buffer)
            shutil.copyfileobj(errors, sys.stderr.buffer)
            return result.returncode
        raw_bytes = output.read()
        error_bytes = errors.read(MAX_COMPRESS_BYTES + 1)
        if error_bytes:  # preserve warnings too; no simplification on stderr
            sys.stdout.buffer.write(raw_bytes)
            sys.stderr.buffer.write(error_bytes)
            shutil.copyfileobj(errors, sys.stderr.buffer)
            return result.returncode
        try:
            raw = raw_bytes.decode('utf-8', errors='strict')
        except UnicodeError:
            sys.stdout.buffer.write(raw_bytes)
            return result.returncode
        try:
            from bash_compress import compress
            from plugin_env import resolve_snapshot_dir
            from token_estimate import estimate_tokens
            short = compress(command, raw)
            if short != raw and len(short) < len(raw) * 0.9:
                archive = resolve_snapshot_dir() / 'codex-command-output'
                archive.mkdir(parents=True, exist_ok=True)
                target = archive / (uuid.uuid4().hex + '.txt')
                with target.open('xb') as handle:
                    handle.write(raw_bytes)
                short += f'\n[Token Optimizer: full command output saved to {target}]\n'
                if estimate_tokens(short) < estimate_tokens(raw):
                    sys.stdout.buffer.write(short.encode('utf-8'))
                    sys.stdout.buffer.flush()
                    try:
                        from compression_log import log_compression_event
                        log_compression_event(feature='codex_command_compress', original_text=raw,
                            compressed_text=short, session_id=plan.get('session_id'),
                            model=plan.get('model') or 'unknown', command_pattern='read-only command',
                            verified=True, tier='measured')
                    except Exception:
                        pass
                    return 0
        except Exception:
            pass
        sys.stdout.buffer.write(raw_bytes)
        return result.returncode


def main():
    from utf8_io import enforce_utf8_io
    enforce_utf8_io()
    if len(sys.argv) == 3 and sys.argv[1] == '--run':
        try:
            plan = json.loads(base64.b64decode(sys.argv[2]))
        except (ValueError, UnicodeError):
            print('Token Optimizer: malformed plan', file=sys.stderr)
            return 2
        return run(plan)
    from hook_io import read_stdin_hook_input
    result = rewrite(read_stdin_hook_input() or {})
    if result:
        print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
