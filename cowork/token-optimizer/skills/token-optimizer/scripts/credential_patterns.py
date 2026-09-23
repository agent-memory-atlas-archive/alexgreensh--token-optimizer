"""Shared credential detection and redaction for Token Optimizer.

Provides compiled regex patterns for common API keys, tokens, and secrets,
plus scan/redact functions usable by bash compression, read cache, and
tool archive writers.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

# (label, compiled_regex) pairs. Label is used in redaction placeholders.
CREDENTIAL_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    ("AWS access key",          re.compile(r"AKIA[0-9A-Z]{16}")),
    ("OpenAI/Anthropic key",    re.compile(r"sk-[a-zA-Z0-9]{20,}")),
    ("Anthropic key",           re.compile(r"sk-ant-[a-zA-Z0-9\-]{20,}")),
    ("GitHub PAT classic",      re.compile(r"ghp_[a-zA-Z0-9]{36}")),
    ("GitHub OAuth token",      re.compile(r"gho_[a-zA-Z0-9]{36}")),
    ("GitHub server token",     re.compile(r"ghs_[a-zA-Z0-9]{36}")),
    ("GitHub refresh token",    re.compile(r"ghr_[a-zA-Z0-9]{36}")),
    ("GitHub fine-grained PAT", re.compile(r"github_pat_[a-zA-Z0-9_]{80,}")),
    ("npm token",               re.compile(r"npm_[a-zA-Z0-9]{36}")),
    ("Slack bot token",         re.compile(r"xoxb-[0-9]+-[a-zA-Z0-9]+")),
    ("Slack user token",        re.compile(r"xoxp-[0-9]+-[a-zA-Z0-9]+")),
    ("Slack app token",         re.compile(r"xoxa-[0-9]+-[a-zA-Z0-9]+")),
    ("Stripe live key",         re.compile(r"sk_live_[a-zA-Z0-9]{24,}")),
    ("Stripe restricted key",   re.compile(r"rk_live_[a-zA-Z0-9]{24,}")),
    ("HuggingFace token",       re.compile(r"hf_[a-zA-Z0-9]{34}")),
    # M-16: negative lookahead so the Bearer pattern doesn't re-match text
    # inside its own redaction placeholder [CREDENTIAL REDACTED: Bearer token].
    # The lookahead checks the text BEFORE Bearer, but Python regex doesn't
    # support variable-width lookbehinds. Instead, redact_credentials protects
    # placeholders with a sentinel before running patterns. The lookahead here
    # is a defense-in-depth for direct pattern.search() callers.
    ("Bearer token",            re.compile(r"Bearer\s+[a-zA-Z0-9\-._~+/]+=*", re.I)),
    ("Google API key",          re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    ("Google OAuth token",      re.compile(r"ya29\.[0-9A-Za-z_\-]{20,}")),
    ("JWT",                     re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("PEM private key",         re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("Database URI",            re.compile(r"(?:postgres|postgresql|mysql|mongodb|mongodb\+srv|redis)://[^:\s/]+:[^@\s]+@", re.I)),
    ("HTTP basic auth URL",     re.compile(r"https?://[^:\s/@]+:[^@\s]+@", re.I)),
    # Credentials passed as URL query/matrix parameters OR OAuth-implicit-flow
    # fragment params (e.g. ?token=..., ?api_key=..., ;password=..., #access_token=...).
    # The named `keep` group captures the "?name="/"#name=" prefix so redaction
    # preserves the parameter name and blanks only the value (see redact_credentials).
    # The value class stops at the next delimiter (& # ; whitespace quote < >) but
    # otherwise matches EVERYTHING — including brackets — so a secret that itself
    # contains a `[` (common in passwords) is redacted whole, not leaked past the
    # bracket. To still avoid re-wrapping an already-inserted "[CREDENTIAL REDACTED:
    # ...]" placeholder (when the value is itself another credential shape an earlier
    # pattern redacted, e.g. ?token=<Bearer ...>), a negative lookahead skips a value
    # that begins with the placeholder rather than excluding brackets from real values.
    ("URL auth param",          re.compile(
        r"(?P<keep>[?&#;](?:authorization|access[_-]?token|refresh[_-]?token|client[_-]?secret"
        r"|session[_-]?token|id[_-]?token|api[_-]?key|sessionid|session|password|passwd|signature"
        r"|secret|bearer|token|auth|sig|pwd|key|jwt)=)(?!\[CREDENTIAL REDACTED:)[^&#;\s\"'<>]+",
        re.I,
    )),
    # M-12: mysql -p<password> (inline password after -p with no space).
    # The -p flag is special: the password immediately follows with no = or space.
    # N-3: re.I so "MySQL -pSECRET" (capitalized client name, as MySQL ships
    # it) is redacted too; the anchor gate already lowercases, so gating is
    # unaffected.
    ("MySQL password flag",     re.compile(
        r"(?P<keep>\bmysql\s+.*?(?<!\S)(?-i:-p)\s*)(?!-)(?!\[CREDENTIAL REDACTED:)"
        r"(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s\"']+)",
        re.I,
    )),
    # M-12: PGPASSWORD=, MYSQL_PWD=, and similar *_PASSWORD= / *_PWD= env assignments.
    # These appear as shell command prefixes (FOO=bar cmd ...) or in config output.
    ("Database env password",   re.compile(
        r"(?P<keep>\b(?:PGPASSWORD|MYSQL_PWD|REDIS_PASSWORD|MONGO_PASSWORD|DB_PASSWORD"
        r"|DATABASE_PASSWORD|PGPASSWD)=[\"\']?)(?!\[CREDENTIAL REDACTED:)[^\s\"'\n]+",
        re.I,
    )),
    # M-12: AWS secret access key (40-char base64). Distinct from the access key
    # (AKIA prefix). Secret keys are mixed-case base64, 40 chars, no prefix.
    # Use a context prefix to avoid false positives on random 40-char base64
    # strings. No trailing \b because the secret may end with = or + (non-word).
    ("AWS secret key",          re.compile(
        r"(?P<keep>\b(?:aws_secret_access_key|aws_secret|secret_access_key|SecretAccessKey)[\"\'\s:=]+)"
        r"(?!\[CREDENTIAL REDACTED:)[A-Za-z0-9/+=]{40}",
        re.I,
    )),
    # Inline CLI password flags. Two patterns:
    # (a) Long forms (--password=V, --password V, --passwd=V, --passcode=V,
    #     --auth-token=V) — unambiguous, always redact.
    # (b) Short forms (-p V, -a V) restricted to known password-carrying
    #     commands (sshpass, mysql, mariadb, redis-cli) to avoid false
    #     positives on -p port/plugin/preserve flags in other commands.
    # The named `keep` group captures the flag (+ command context for short
    # forms) so redaction preserves it and blanks only the value.
    ("CLI password flag (long)", re.compile(
        r"(?P<keep>(?:--password|--passwd|--passcode|--auth-token)(?![\w-])(?:\s*=\s*|\s+))"
        r"(?!-)(?!\[CREDENTIAL REDACTED:)"
        r"(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s\"']+)",
        re.I,
    )),
    ("CLI password flag (short)", re.compile(
        r"(?P<keep>sshpass\b.*?(?<!\S)(?-i:-p)\s*"
        r"|redis-cli\b.*?(?<!\S)(?-i:-a)\s+"
        r"|mariadb\b.*?(?<!\S)(?-i:-p)\s*)"
        r"(?!-)(?!\[CREDENTIAL REDACTED:)"
        r"(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s\"']+)",
        re.I,
    )),
]

# Bare compiled patterns list for backward compat with bash_compress.py
PATTERNS_ONLY: List["re.Pattern[str]"] = [pat for _, pat in CREDENTIAL_PATTERNS]

# ---------------------------------------------------------------------------
# H-8: fast pre-check for credential redaction.
#
# The old redact_credentials ran 23 sequential re.sub() calls on the full
# text unconditionally: 97ms for 10K lines, 675ms for 50K lines, even when
# the text contained NO credentials (the common case for command output).
# Python's re engine uses backtracking, not a DFA, so combining all
# patterns into a single alternation is actually SLOWER (154ms for 10K
# clean lines) due to the complex NFA state per character.
#
# The fix: a fast prefix scan using simple string containment checks
# before any regex runs. If none of the credential prefixes appear in the
# text, skip all 23 re.sub() calls entirely. This makes clean text O(n)
# with a tiny constant (a single str.find pass per prefix), while text
# with credentials still gets the full sequential redaction (correctness
# preserved, no regex complexity change).
#
# The prefix list is derived from the literal prefixes of each pattern:
# "AKIA", "sk-", "ghp_", "gho_", "ghs_", "ghr_", "github_pat_", "npm_",
# "xoxb-", "xoxp-", "xoxa-", "sk_live_", "rk_live_", "hf_", "Bearer",
# "AIza", "ya29.", "eyJ", "-----BEGIN", and the URL scheme prefixes for
# database/basic-auth URIs. The URL auth param pattern has no single
# literal prefix (it matches parameter names), so we check for "=" as a
# coarse pre-filter — but only if other prefixes didn't already match.
# ---------------------------------------------------------------------------
_CREDENTIAL_PREFIXES: Tuple[str, ...] = (
    "AKIA", "sk-", "ghp_", "gho_", "ghs_", "ghr_", "github_pat_",
    "npm_", "xoxb-", "xoxp-", "xoxa-", "sk_live_", "rk_live_", "hf_",
    "Bearer", "bearer", "AIza", "ya29.", "eyJ",
    "-----BEGIN",  # PEM private key
    "postgres://", "postgresql://", "mysql://", "mongodb://",
    "mongodb+srv://", "redis://",  # database URI
    "http://", "https://",  # basic auth URL (coarse, but covers the pattern)
    # M-12: new credential prefixes
    "mysql ",  # mysql -p<password>
    "PGPASSWORD=", "MYSQL_PWD=", "REDIS_PASSWORD=", "MONGO_PASSWORD=",
    "DB_PASSWORD=", "DATABASE_PASSWORD=", "PGPASSWD=",
    "aws_secret", "secret_access_key", "SecretAccessKey",
)
# URL auth param parameter names (lowercase, checked case-insensitively).
_URL_AUTH_PARAM_NAMES: Tuple[str, ...] = (
    "authorization=", "access_token=", "access-token=", "refresh_token=",
    "refresh-token=", "client_secret=", "client-secret=", "session_token=",
    "session-token=", "id_token=", "id-token=", "api_key=", "api-key=",
    "sessionid=", "session=", "password=", "passwd=", "signature=",
    "secret=", "bearer=", "token=", "auth=", "sig=", "pwd=", "key=",
    "jwt=",
)


def _text_may_contain_credentials(text: str) -> bool:
    """Fast prefix scan: return True if any credential prefix appears in text.

    This is a coarse pre-filter using str.find (C-level, no regex engine).
    False negatives would be a security bug, so every prefix is checked.
    False positives are fine — the full regex suite runs and finds nothing.
    """
    for prefix in _CREDENTIAL_PREFIXES:
        if prefix in text:
            return True
    # URL auth param names are case-insensitive in the pattern. Use a
    # lowercase copy for the check.
    lower = text.lower()
    for name in _URL_AUTH_PARAM_NAMES:
        if name in lower:
            return True
    return False


# ---------------------------------------------------------------------------
# Custom (user-defined) redaction patterns.
#
# Organizations have secret shapes no built-in list can know about: internal
# API keys, service tokens, record identifiers. Users add them in a JSON file
# instead of editing this module:
#
#   <runtime-home>/token-optimizer/redact-patterns.json   (default location)
#
# or point TOKEN_OPTIMIZER_REDACT_PATTERNS_FILE at another path (process env
# first, then the settings.json "env" block, like the other TOKEN_OPTIMIZER_*
# knobs). Format:
#
#   {"patterns": [
#       "acme_[A-Za-z0-9]{32}",
#       {"label": "Acme service token", "regex": "(?P<keep>ACME_TOKEN=)\\S+",
#        "ignore_case": true}
#   ]}
#
# Custom patterns are ADDITIVE: every built-in pattern still runs first, and
# custom patterns run after it on the text with built-in placeholders
# protected. They apply to redact_credentials() and scan_for_credentials(),
# i.e. everything written to disk. CREDENTIAL_PATTERNS and PATTERNS_ONLY stay
# the built-in set (import-time constants, used by compressors to decide which
# lines to keep verbatim).
#
# Loading is lazy (first redaction call), cached per process, and never raises:
# a bad entry is skipped with one stderr warning, so a typo cannot break a hook.
# ---------------------------------------------------------------------------
CUSTOM_PATTERNS_FILE_ENV = "TOKEN_OPTIMIZER_REDACT_PATTERNS_FILE"
CUSTOM_PATTERNS_FILENAME = "redact-patterns.json"
_CUSTOM_DEFAULT_LABEL = "custom pattern"
_CUSTOM_MAX_FILE_BYTES = 1_048_576
_CUSTOM_MAX_PATTERNS = 200
_CUSTOM_MAX_REGEX_CHARS = 1000
_CUSTOM_MAX_LABEL_CHARS = 60
# Characters that would break the "[CREDENTIAL REDACTED: <label>]" placeholder
# (and _PLACEHOLDER_RE, which stops at "]") or the one-line archive format.
_LABEL_UNSAFE_RE = re.compile(r"[\]\[\x00-\x1f\x7f]")

_BUILTIN_KEYS = frozenset((pat.pattern, pat.flags) for _, pat in CREDENTIAL_PATTERNS)


class _CustomPatternState:
    """Result of one load: the compiled patterns, where they came from, and
    human-readable problems (for the security report and stderr)."""

    __slots__ = ("patterns", "source", "errors", "duplicates")

    def __init__(self) -> None:
        self.patterns: List[Tuple[str, "re.Pattern[str]"]] = []
        self.source: Optional[str] = None
        self.errors: List[str] = []
        self.duplicates: int = 0


_CUSTOM_STATE: Optional[_CustomPatternState] = None


def _warn(msg: str) -> None:
    try:
        print(f"[token-optimizer] {msg}", file=sys.stderr)
    except Exception:
        pass


def _settings_env_value(name: str) -> str:
    """Read ``name`` from the settings.json "env" block. Never raises."""
    try:
        from runtime_env import claude_home
        path = claude_home() / "settings.json"
        if path.stat().st_size > _CUSTOM_MAX_FILE_BYTES:
            return ""
        with open(path, "r", encoding="utf-8") as f:
            settings = json.load(f)
        env_block = settings.get("env", {}) if isinstance(settings, dict) else {}
        if not isinstance(env_block, dict):
            return ""
        return str(env_block.get(name, "") or "").strip()
    except Exception:
        return ""


def _default_custom_patterns_path() -> Optional[Path]:
    try:
        from runtime_env import runtime_home
        return runtime_home() / "token-optimizer" / CUSTOM_PATTERNS_FILENAME
    except Exception:
        return None


def _resolve_custom_patterns_path() -> Tuple[Optional[Path], bool]:
    """Return (path, explicit). ``explicit`` is True when the user named the
    file via TOKEN_OPTIMIZER_REDACT_PATTERNS_FILE, so a missing file warns."""
    raw = os.environ.get(CUSTOM_PATTERNS_FILE_ENV, "").strip()
    if not raw:
        raw = _settings_env_value(CUSTOM_PATTERNS_FILE_ENV)
    if raw:
        return Path(os.path.expanduser(os.path.expandvars(raw))), True
    return _default_custom_patterns_path(), False


def _clean_label(raw: object) -> str:
    if not isinstance(raw, str):
        return _CUSTOM_DEFAULT_LABEL
    label = _LABEL_UNSAFE_RE.sub("", raw).strip()
    label = " ".join(label.split())[:_CUSTOM_MAX_LABEL_CHARS].strip()
    return label or _CUSTOM_DEFAULT_LABEL


def _compile_custom_entry(entry: object, index: int, state: _CustomPatternState,
                          seen: set) -> None:
    where = f"entry {index}"
    if isinstance(entry, str):
        regex, label, ignore_case = entry, _CUSTOM_DEFAULT_LABEL, False
    elif isinstance(entry, dict):
        regex = entry.get("regex")
        label = _clean_label(entry.get("label"))
        ignore_case = entry.get("ignore_case", False)
        if not isinstance(ignore_case, bool):
            state.errors.append(f"{where}: ignore_case must be true or false")
            return
    else:
        state.errors.append(f"{where}: must be a string or an object with a \"regex\" key")
        return
    if not isinstance(regex, str) or not regex.strip():
        state.errors.append(f"{where}: regex is missing or empty")
        return
    if len(regex) > _CUSTOM_MAX_REGEX_CHARS:
        state.errors.append(f"{where}: regex longer than {_CUSTOM_MAX_REGEX_CHARS} characters")
        return
    try:
        pat = re.compile(regex, re.I if ignore_case else 0)
    except (re.error, RecursionError, OverflowError, ValueError) as exc:
        state.errors.append(f"{where}: invalid regex ({exc})")
        return
    # A pattern that matches the empty string would insert a placeholder
    # between every character of every archived output.
    try:
        matches_empty = pat.fullmatch("") is not None or pat.search("") is not None
    except Exception:
        matches_empty = True
    if matches_empty:
        state.errors.append(f"{where}: regex matches empty text")
        return
    key = (pat.pattern, pat.flags)
    if key in _BUILTIN_KEYS or key in seen:
        state.duplicates += 1
        return
    seen.add(key)
    state.patterns.append((label, pat))


def _load_custom_patterns() -> _CustomPatternState:
    state = _CustomPatternState()
    try:
        path, explicit = _resolve_custom_patterns_path()
        if path is None:
            return state
        state.source = str(path)
        try:
            if not path.is_file():
                if explicit:
                    state.errors.append(f"{CUSTOM_PATTERNS_FILE_ENV} points to a missing file")
                return state
            if path.stat().st_size > _CUSTOM_MAX_FILE_BYTES:
                state.errors.append("file is larger than 1 MB; ignored")
                return state
            # utf-8-sig: Windows Notepad saves UTF-8 with a BOM.
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            state.errors.append(f"could not read file ({exc.__class__.__name__}: {exc})")
            return state
        if isinstance(data, dict):
            entries = data.get("patterns")
        else:
            entries = None
        if not isinstance(entries, list):
            state.errors.append('top level must be an object with a "patterns" list')
            return state
        if len(entries) > _CUSTOM_MAX_PATTERNS:
            state.errors.append(
                f"{len(entries)} patterns listed; only the first {_CUSTOM_MAX_PATTERNS} are used"
            )
            entries = entries[:_CUSTOM_MAX_PATTERNS]
        seen: set = set()
        for i, entry in enumerate(entries, 1):
            _compile_custom_entry(entry, i, state, seen)
    except Exception as exc:  # pragma: no cover - defense in depth
        state.errors.append(f"unexpected error ({exc.__class__.__name__})")
    return state


def _custom_state() -> _CustomPatternState:
    global _CUSTOM_STATE
    if _CUSTOM_STATE is None:
        state = _load_custom_patterns()
        _CUSTOM_STATE = state
        for err in state.errors:
            _warn(f"custom redaction patterns ({state.source}): {err}")
    return _CUSTOM_STATE


def get_custom_patterns() -> List[Tuple[str, "re.Pattern[str]"]]:
    """User-defined (label, compiled_regex) pairs, loaded once per process."""
    return list(_custom_state().patterns)


def custom_patterns_status() -> dict:
    """Summary for diagnostics: count, labels, source path, problems."""
    state = _custom_state()
    return {
        "count": len(state.patterns),
        "labels": [label for label, _ in state.patterns],
        "source": state.source,
        "errors": list(state.errors),
        "duplicates_skipped": state.duplicates,
    }


def reset_custom_patterns_cache() -> None:
    """Forget the loaded custom patterns (tests, long-lived processes)."""
    global _CUSTOM_STATE
    _CUSTOM_STATE = None


def scan_for_credentials(text: str) -> List[Tuple[str, str, int]]:
    """Scan text for credentials. Returns [(label, matched_text, line_number), ...]."""
    results = []
    all_patterns = CREDENTIAL_PATTERNS + get_custom_patterns()
    for line_num, line in enumerate(text.splitlines()):
        for label, pat in all_patterns:
            m = pat.search(line)
            if m:
                results.append((label, m.group(), line_num))
    return results


# M-16: regex to find already-redacted placeholders so they can be protected
# from re-matching during a second redaction pass.
_PLACEHOLDER_RE = re.compile(r"\[CREDENTIAL REDACTED: [^\]]+\]")
_PLACEHOLDER_SENTINEL = "\x00\x01REDACTED\x00\x01"

# Per-pattern literal anchors (checked on a lowercased copy of the ORIGINAL
# text, once, before the loop). A pattern whose anchors are all absent cannot
# match, so its re.sub() is skipped. This is what makes clean-but-URL-bearing
# output cheap: the global prefix scan above fires on any "https://", after
# which the three M-12 patterns and the two URI patterns used to cost more
# than everything else combined (measured 24ms -> 55ms per 10K realistic
# lines on the first H-8 attempt). Substitutions only remove secrets and add
# placeholders, never new anchors, so a pre-loop check stays sound.
_PATTERN_ANCHORS = {
    # Only the patterns that are expensive to run (case-insensitive
    # alternations, or a scheme scan) are gated. The literal-prefix patterns
    # (AKIA..., ghp_..., xoxb-...) are already a fast scan in the regex engine
    # and cost less than an extra anchor check would.
    "Bearer token": ("bearer",),
    "Database URI": ("://",),
    "HTTP basic auth URL": ("://",),
    "URL auth param": ("=",),
    "MySQL password flag": ("mysql",),
    "Database env password": ("password=", "pwd=", "passwd=",
                              "password='", "password=\"", "pwd='", "pwd=\"",
                              "passwd='", "passwd=\""),
    "AWS secret key": ("aws_secret", "secret_access_key", "secretaccesskey"),
    "CLI password flag (long)": ("--password", "--passwd", "--passcode", "--auth-token"),
    "CLI password flag (short)": ("sshpass", "mariadb", "redis-cli"),
}


def _sub_with_placeholder(pat: "re.Pattern[str]", label: str, text: str) -> str:
    # A function replacement, not a template string: a custom label is user
    # text and must never be interpreted as a backreference ("\\1", "\\g<0>").
    placeholder = f"[CREDENTIAL REDACTED: {label}]"
    if "keep" in pat.groupindex:
        def _repl(m):
            if m.group("keep") is None:
                if m.start() == m.end():
                    return m.group(0)
                return placeholder
            ks, ke = m.span("keep")
            # The kept group covers the whole match: nothing to redact.
            if ks == m.start() and ke == m.end():
                return m.group(0)
            # Keep the group where it sits; replace what comes before and/or
            # after it. Built-in patterns always put `keep` first, so for them
            # this is exactly "<keep>[CREDENTIAL REDACTED: label]".
            return ((placeholder if ks > m.start() else "")
                    + m.group("keep")
                    + (placeholder if ke < m.end() else ""))
    else:
        def _repl(m):
            # Zero-width matches (\b, lookaheads) would otherwise sprinkle
            # placeholders between characters without removing anything.
            if m.start() == m.end():
                return m.group(0)
            return placeholder
    return pat.sub(_repl, text)


def redact_credentials(text: str) -> str:
    """Replace credential matches with [CREDENTIAL REDACTED: <type>] placeholders.

    A pattern may define a named `keep` group for a non-secret prefix that should
    survive redaction (e.g. the "?token=" part of a URL auth parameter); only the
    value after it is replaced. Patterns without a `keep` group redact the whole
    match, unchanged.

    H-8: uses a fast prefix scan to skip all re.sub() calls when the text
    contains no credential prefixes (the common case for clean command output).
    This makes clean text O(n) with a tiny constant instead of O(n × 23) regex
    passes. Text with credentials still gets the full sequential redaction,
    preserving correctness and the existing two-phase ordering (standalone
    credentials before URL auth params, so the negative lookahead works).

    M-16: protects already-redacted [CREDENTIAL REDACTED: ...] placeholders
    from re-matching by replacing them with a sentinel before redaction and
    restoring them after. This fixes the Bearer pattern re-matching "Bearer
    token" inside its own placeholder, which nested placeholders on re-runs.
    """
    # M-16: protect existing placeholders from re-matching.
    placeholders = []
    def _save_placeholder(m):
        placeholders.append(m.group(0))
        return _PLACEHOLDER_SENTINEL
    if "[CREDENTIAL REDACTED:" in text:
        text = _PLACEHOLDER_RE.sub(_save_placeholder, text)

    # H-8: fast path — skip all regex work if no credential prefix is present.
    lowered = text.lower()
    for label, pat in CREDENTIAL_PATTERNS:
        anchors = _PATTERN_ANCHORS.get(label)
        if anchors and not any(a in lowered for a in anchors):
            continue
        text = _sub_with_placeholder(pat, label, text)

    # M-16: restore protected placeholders.
    for ph in placeholders:
        text = text.replace(_PLACEHOLDER_SENTINEL, ph, 1)

    # Custom patterns run after every built-in, and only on the text BETWEEN
    # placeholders, so a broad custom regex can never match inside an existing
    # "[CREDENTIAL REDACTED: ...]" (built-in or pre-existing) and corrupt it.
    custom = get_custom_patterns()
    if custom:
        text = _redact_custom(text, custom)
    return text


def _redact_custom(text: str, custom: List[Tuple[str, "re.Pattern[str]"]]) -> str:
    for label, pat in custom:
        parts = []
        last = 0
        for m in _PLACEHOLDER_RE.finditer(text):
            parts.append(_sub_with_placeholder(pat, label, text[last:m.start()]))
            parts.append(m.group(0))
            last = m.end()
        parts.append(_sub_with_placeholder(pat, label, text[last:]))
        text = "".join(parts)
    return text
