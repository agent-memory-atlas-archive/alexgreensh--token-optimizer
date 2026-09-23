"""Tests for user-defined credential redaction patterns (credential_patterns.py).

Custom patterns come from a JSON file (default
<runtime-home>/token-optimizer/redact-patterns.json, or the path in
TOKEN_OPTIMIZER_REDACT_PATTERNS_FILE). They are additive to the built-in set,
never raise, and never corrupt existing placeholders.

Each behavior is paired with a negative control: the same input WITHOUT the
custom file keeps the value, so a passing test proves the custom pattern did
the redaction.

All fixtures are invented; no real secret appears here.

Run: python3 -m pytest tests/test_custom_redaction_patterns.py -v
"""
import json
import os
import sys

import pytest

_SCRIPTS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "skills", "token-optimizer", "scripts",
)
sys.path.insert(0, _SCRIPTS)

import credential_patterns as cp  # noqa: E402

ENV = cp.CUSTOM_PATTERNS_FILE_ENV
ORG_KEY = "medx_" + "A1b2C3d4" * 4          # invented org key shape
ORG_KEY_RE = r"medx_[A-Za-z0-9]{32}"
AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"       # invented, matches built-in shape


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Point every lookup at an empty temp home and reset the cache."""
    monkeypatch.delenv(ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    (tmp_path / "home").mkdir()
    (tmp_path / "claude").mkdir()
    # Pin the default-location resolvers to temp dirs so a developer's real
    # ~/.claude/token-optimizer/redact-patterns.json can never leak in.
    monkeypatch.setattr(cp, "_default_custom_patterns_path",
                        lambda: tmp_path / "claude" / "token-optimizer" / cp.CUSTOM_PATTERNS_FILENAME)
    monkeypatch.setattr(cp, "_settings_env_value", lambda name: "")
    cp.reset_custom_patterns_cache()
    yield
    cp.reset_custom_patterns_cache()


def _write(tmp_path, data, name="patterns.json", raw=None, encoding="utf-8"):
    path = tmp_path / name
    if raw is not None:
        path.write_bytes(raw)
    else:
        path.write_text(json.dumps(data), encoding=encoding)
    return path


def _use(monkeypatch, path):
    monkeypatch.setenv(ENV, str(path))
    cp.reset_custom_patterns_cache()


# --------------------------------------------------------------------------
# Core behavior + negative controls
# --------------------------------------------------------------------------

def test_negative_control_org_key_survives_without_custom_file():
    out = cp.redact_credentials(f"token {ORG_KEY} end")
    assert ORG_KEY in out
    assert cp.custom_patterns_status()["count"] == 0


def test_custom_string_pattern_redacts(tmp_path, monkeypatch):
    _use(monkeypatch, _write(tmp_path, {"patterns": [ORG_KEY_RE]}))
    out = cp.redact_credentials(f"token {ORG_KEY} end")
    assert ORG_KEY not in out
    assert out == "token [CREDENTIAL REDACTED: custom pattern] end"


def test_custom_object_pattern_uses_label(tmp_path, monkeypatch):
    _use(monkeypatch, _write(tmp_path, {"patterns": [
        {"label": "MedX API key", "regex": ORG_KEY_RE}]}))
    out = cp.redact_credentials(ORG_KEY)
    assert out == "[CREDENTIAL REDACTED: MedX API key]"


def test_default_location_is_used_without_env(tmp_path, monkeypatch):
    default = cp._default_custom_patterns_path()
    default.parent.mkdir(parents=True)
    default.write_text(json.dumps({"patterns": [ORG_KEY_RE]}), encoding="utf-8")
    cp.reset_custom_patterns_cache()
    assert ORG_KEY not in cp.redact_credentials(ORG_KEY)
    assert cp.custom_patterns_status()["source"] == str(default)


def test_env_path_wins_over_default(tmp_path, monkeypatch):
    default = cp._default_custom_patterns_path()
    default.parent.mkdir(parents=True)
    default.write_text(json.dumps({"patterns": [r"never_[0-9]+"]}), encoding="utf-8")
    _use(monkeypatch, _write(tmp_path, {"patterns": [ORG_KEY_RE]}))
    assert ORG_KEY not in cp.redact_credentials(ORG_KEY)
    assert "never_123" in cp.redact_credentials("never_123")


def test_settings_json_env_block_is_honored(tmp_path, monkeypatch):
    path = _write(tmp_path, {"patterns": [ORG_KEY_RE]})
    monkeypatch.setattr(cp, "_settings_env_value",
                        lambda name: str(path) if name == ENV else "")
    cp.reset_custom_patterns_cache()
    assert ORG_KEY not in cp.redact_credentials(ORG_KEY)


def test_real_settings_json_reader(tmp_path, monkeypatch):
    """Exercise the unpatched settings.json reader against CLAUDE_CONFIG_DIR."""
    monkeypatch.undo()  # drop the fixture's _settings_env_value stub
    monkeypatch.delenv(ENV, raising=False)
    claude = tmp_path / "claude2"
    claude.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    path = _write(tmp_path, {"patterns": [ORG_KEY_RE]})
    (claude / "settings.json").write_text(json.dumps({"env": {ENV: str(path)}}), encoding="utf-8")
    assert cp._settings_env_value(ENV) == str(path)
    (claude / "settings.json").write_text("{not json", encoding="utf-8")
    assert cp._settings_env_value(ENV) == ""
    (claude / "settings.json").write_text(json.dumps({"env": ["x"]}), encoding="utf-8")
    assert cp._settings_env_value(ENV) == ""


def test_scan_for_credentials_reports_custom(tmp_path, monkeypatch):
    _use(monkeypatch, _write(tmp_path, {"patterns": [
        {"label": "MedX API key", "regex": ORG_KEY_RE}]}))
    hits = cp.scan_for_credentials(f"line one\nkey={ORG_KEY}")
    assert ("MedX API key", ORG_KEY, 1) in hits


def test_ignore_case_flag(tmp_path, monkeypatch):
    _use(monkeypatch, _write(tmp_path, {"patterns": [
        {"regex": r"medx-internal-[0-9]{6}", "ignore_case": True}]}))
    assert "MEDX-INTERNAL-123456" not in cp.redact_credentials("MEDX-INTERNAL-123456")
    # negative control: without ignore_case the upper-case form survives
    _use(monkeypatch, _write(tmp_path, {"patterns": [r"medx-internal-[0-9]{6}"]}, name="p2.json"))
    assert "MEDX-INTERNAL-123456" in cp.redact_credentials("MEDX-INTERNAL-123456")


def test_keep_group_preserves_prefix(tmp_path, monkeypatch):
    _use(monkeypatch, _write(tmp_path, {"patterns": [
        {"label": "MedX token", "regex": r"(?P<keep>MEDX_TOKEN=)\S+"}]}))
    out = cp.redact_credentials("export MEDX_TOKEN=s3cr3tvalue now")
    assert out == "export MEDX_TOKEN=[CREDENTIAL REDACTED: MedX token] now"


def test_bom_file_is_read(tmp_path, monkeypatch):
    """Windows Notepad writes UTF-8 with a BOM."""
    raw = b"\xef\xbb\xbf" + json.dumps({"patterns": [ORG_KEY_RE]}).encode()
    _use(monkeypatch, _write(tmp_path, None, raw=raw))
    assert ORG_KEY not in cp.redact_credentials(ORG_KEY)
    assert cp.custom_patterns_status()["errors"] == []


def test_tilde_path_is_expanded(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "cfg.json").write_text(json.dumps({"patterns": [ORG_KEY_RE]}), encoding="utf-8")
    monkeypatch.setenv(ENV, "~/cfg.json")
    cp.reset_custom_patterns_cache()
    assert ORG_KEY not in cp.redact_credentials(ORG_KEY)


# --------------------------------------------------------------------------
# Interaction with built-in patterns
# --------------------------------------------------------------------------

def test_builtins_still_run_with_custom_file(tmp_path, monkeypatch):
    _use(monkeypatch, _write(tmp_path, {"patterns": [ORG_KEY_RE]}))
    out = cp.redact_credentials(f"{AWS_KEY} {ORG_KEY}")
    assert out == ("[CREDENTIAL REDACTED: AWS access key] "
                   "[CREDENTIAL REDACTED: custom pattern]")


def test_builtin_label_wins_on_overlap(tmp_path, monkeypatch):
    """Built-ins run first; a custom regex covering a built-in shape does not relabel it."""
    _use(monkeypatch, _write(tmp_path, {"patterns": [
        {"label": "any AKIA", "regex": r"AKIA[0-9A-Z]+"}]}))
    assert cp.redact_credentials(AWS_KEY) == "[CREDENTIAL REDACTED: AWS access key]"


def test_broad_custom_pattern_cannot_corrupt_placeholders(tmp_path, monkeypatch):
    """A custom regex that matches words inside a placeholder must not touch it."""
    _use(monkeypatch, _write(tmp_path, {"patterns": [
        {"label": "caps", "regex": r"[A-Z]{5,}"}]}))
    out = cp.redact_credentials(f"{AWS_KEY} and SECRETWORD")
    assert out == "[CREDENTIAL REDACTED: AWS access key] and [CREDENTIAL REDACTED: caps]"
    # idempotent: a second pass over already-redacted text changes nothing
    assert cp.redact_credentials(out) == out


def test_preexisting_placeholder_untouched(tmp_path, monkeypatch):
    _use(monkeypatch, _write(tmp_path, {"patterns": [r"REDACTED"]}))
    text = "x [CREDENTIAL REDACTED: JWT] y"
    assert cp.redact_credentials(text) == text


def test_builtin_constants_unchanged(tmp_path, monkeypatch):
    before = list(cp.CREDENTIAL_PATTERNS)
    before_only = list(cp.PATTERNS_ONLY)
    _use(monkeypatch, _write(tmp_path, {"patterns": [ORG_KEY_RE]}))
    cp.redact_credentials(ORG_KEY)
    assert cp.CREDENTIAL_PATTERNS == before
    assert cp.PATTERNS_ONLY == before_only


def test_duplicate_of_builtin_is_skipped(tmp_path, monkeypatch):
    label, builtin = cp.CREDENTIAL_PATTERNS[0]
    _use(monkeypatch, _write(tmp_path, {"patterns": [builtin.pattern, ORG_KEY_RE, ORG_KEY_RE]}))
    status = cp.custom_patterns_status()
    assert status["count"] == 1
    assert status["duplicates_skipped"] == 2
    assert status["errors"] == []
    # the built-in still redacts under its own label
    assert cp.redact_credentials(AWS_KEY) == f"[CREDENTIAL REDACTED: {label}]"


def test_same_regex_different_flags_is_not_a_duplicate(tmp_path, monkeypatch):
    _use(monkeypatch, _write(tmp_path, {"patterns": [
        r"abc_[0-9]{4}", {"regex": r"abc_[0-9]{4}", "ignore_case": True}]}))
    assert cp.custom_patterns_status()["count"] == 2


# --------------------------------------------------------------------------
# Bad input: never raises, skips the entry, reports why
# --------------------------------------------------------------------------

def test_missing_default_file_is_silent(capsys):
    assert cp.redact_credentials("plain text") == "plain text"
    status = cp.custom_patterns_status()
    assert status["count"] == 0 and status["errors"] == []
    assert capsys.readouterr().err == ""


def test_missing_explicit_file_warns_once(tmp_path, monkeypatch, capsys):
    _use(monkeypatch, tmp_path / "nope.json")
    assert cp.redact_credentials(ORG_KEY) == ORG_KEY
    cp.redact_credentials(ORG_KEY)
    err = capsys.readouterr().err
    assert err.count("missing file") == 1
    assert cp.custom_patterns_status()["errors"]


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_env_value_falls_back_to_default(tmp_path, monkeypatch, value):
    monkeypatch.setenv(ENV, value)
    cp.reset_custom_patterns_cache()
    status = cp.custom_patterns_status()
    assert status["source"] == str(cp._default_custom_patterns_path())
    assert status["errors"] == []


def test_invalid_regex_skipped_others_still_load(tmp_path, monkeypatch, capsys):
    _use(monkeypatch, _write(tmp_path, {"patterns": ["([unclosed", ORG_KEY_RE]}))
    assert ORG_KEY not in cp.redact_credentials(ORG_KEY)
    status = cp.custom_patterns_status()
    assert status["count"] == 1
    assert any("entry 1" in e and "invalid regex" in e for e in status["errors"])
    assert "invalid regex" in capsys.readouterr().err


@pytest.mark.parametrize("entry, needle", [
    ("", "missing or empty"),
    ("   ", "missing or empty"),
    ({"label": "x"}, "missing or empty"),
    ({"regex": ""}, "missing or empty"),
    ({"regex": 42}, "missing or empty"),
    (42, "must be a string or an object"),
    (None, "must be a string or an object"),
    (["a"], "must be a string or an object"),
    ({"regex": "abc", "ignore_case": "yes"}, "ignore_case"),
    ("a*", "matches empty text"),
    ("(?:)", "matches empty text"),
    ("^", "matches empty text"),
    ("x" * 1001, "longer than"),
])
def test_bad_entries_are_skipped(tmp_path, monkeypatch, entry, needle):
    _use(monkeypatch, _write(tmp_path, {"patterns": [entry]}))
    assert cp.redact_credentials("hello world") == "hello world"
    status = cp.custom_patterns_status()
    assert status["count"] == 0
    assert any(needle in e for e in status["errors"]), status["errors"]


@pytest.mark.parametrize("raw", [
    b"{not json",
    b"",
    b"[]",
    b'["abc"]',
    b'{"patterns": "abc"}',
    b'{"other": []}',
    b"\xff\xfe\x00bad",
])
def test_malformed_files_never_raise(tmp_path, monkeypatch, raw):
    _use(monkeypatch, _write(tmp_path, None, raw=raw))
    assert cp.redact_credentials(f"{AWS_KEY}") == "[CREDENTIAL REDACTED: AWS access key]"
    status = cp.custom_patterns_status()
    assert status["count"] == 0
    assert status["errors"]


def test_empty_patterns_list_is_fine(tmp_path, monkeypatch):
    _use(monkeypatch, _write(tmp_path, {"patterns": []}))
    status = cp.custom_patterns_status()
    assert status["count"] == 0 and status["errors"] == []


def test_directory_path_is_reported(tmp_path, monkeypatch):
    _use(monkeypatch, tmp_path)
    assert cp.custom_patterns_status()["errors"]


def test_oversized_file_ignored(tmp_path, monkeypatch):
    big = {"patterns": [ORG_KEY_RE], "pad": "x" * (cp._CUSTOM_MAX_FILE_BYTES + 10)}
    _use(monkeypatch, _write(tmp_path, big))
    assert ORG_KEY in cp.redact_credentials(ORG_KEY)
    assert any("1 MB" in e for e in cp.custom_patterns_status()["errors"])


def test_pattern_count_cap(tmp_path, monkeypatch):
    entries = [f"capped_{i}_[0-9]{{3}}" for i in range(cp._CUSTOM_MAX_PATTERNS + 5)]
    _use(monkeypatch, _write(tmp_path, {"patterns": entries}))
    status = cp.custom_patterns_status()
    assert status["count"] == cp._CUSTOM_MAX_PATTERNS
    assert any("only the first" in e for e in status["errors"])


# --------------------------------------------------------------------------
# Labels are user text: they must not break the placeholder format
# --------------------------------------------------------------------------

@pytest.mark.parametrize("label, expected", [
    ("Org key]", "Org key"),
    ("[weird] label", "weird label"),
    ("multi\nline\tlabel", "multilinelabel"),
    ("   ", "custom pattern"),
    (123, "custom pattern"),
    ("L" * 100, "L" * 60),
])
def test_label_sanitised(tmp_path, monkeypatch, label, expected):
    _use(monkeypatch, _write(tmp_path, {"patterns": [{"label": label, "regex": ORG_KEY_RE}]}))
    out = cp.redact_credentials(ORG_KEY)
    assert out == f"[CREDENTIAL REDACTED: {expected}]"
    assert cp._PLACEHOLDER_RE.fullmatch(out)


@pytest.mark.parametrize("label", [r"\1", r"\g<0>", r"\g<keep>", "\\"])
def test_label_backslashes_are_literal(tmp_path, monkeypatch, label):
    """Labels must never be interpreted as regex replacement templates."""
    _use(monkeypatch, _write(tmp_path, {"patterns": [
        {"label": label, "regex": r"(?P<keep>ID=)(\d+)"}]}))
    out = cp.redact_credentials("ID=12345")
    assert out == f"ID=[CREDENTIAL REDACTED: {label}]"


# --------------------------------------------------------------------------
# Caching and the archive writer path
# --------------------------------------------------------------------------

def test_loaded_once_per_process(tmp_path, monkeypatch):
    path = _write(tmp_path, {"patterns": [ORG_KEY_RE]})
    _use(monkeypatch, path)
    assert ORG_KEY not in cp.redact_credentials(ORG_KEY)
    path.write_text(json.dumps({"patterns": []}), encoding="utf-8")
    # still cached
    assert ORG_KEY not in cp.redact_credentials(ORG_KEY)
    cp.reset_custom_patterns_cache()
    assert ORG_KEY in cp.redact_credentials(ORG_KEY)


def test_archive_writer_uses_custom_patterns(tmp_path, monkeypatch):
    """The tool-archive redaction wrapper routes through the shared function."""
    import archive_result
    _use(monkeypatch, _write(tmp_path, {"patterns": [ORG_KEY_RE]}))
    out = archive_result._redact_credentials(f"output {ORG_KEY}")
    assert ORG_KEY not in out
    cp.reset_custom_patterns_cache()
    monkeypatch.delenv(ENV)
    cp.reset_custom_patterns_cache()
    assert ORG_KEY in archive_result._redact_credentials(f"output {ORG_KEY}")


# --------------------------------------------------------------------------
# Zero-width matches never insert placeholders
# --------------------------------------------------------------------------

@pytest.mark.parametrize("regex", [r"\b", r"(?=secret)", r"(?<=x)", r"$"])
def test_zero_width_custom_matches_do_nothing(tmp_path, monkeypatch, regex):
    _use(monkeypatch, _write(tmp_path, {"patterns": [regex]}))
    text = "xsecret words here"
    assert cp.redact_credentials(text) == text


def test_keep_group_with_empty_value_does_nothing(tmp_path, monkeypatch):
    _use(monkeypatch, _write(tmp_path, {"patterns": [r"(?P<keep>ID=)\d*"]}))
    assert cp.redact_credentials("ID= and ID=42") == "ID= and ID=[CREDENTIAL REDACTED: custom pattern]"


def test_keep_group_in_suffix_position_stays_in_place(tmp_path, monkeypatch):
    _use(monkeypatch, _write(tmp_path, {"patterns": [
        {"label": "svc", "regex": r"[a-z0-9]{12}(?P<keep>@svc\.medx\.internal)"}]}))
    out = cp.redact_credentials("login a1b2c3d4e5f6@svc.medx.internal ok")
    assert out == "login [CREDENTIAL REDACTED: svc]@svc.medx.internal ok"


def test_optional_keep_group_not_matched_redacts_whole(tmp_path, monkeypatch):
    _use(monkeypatch, _write(tmp_path, {"patterns": [
        {"label": "k", "regex": r"(?P<keep>KEY=)?medx_[0-9]{6}"}]}))
    assert cp.redact_credentials("medx_123456") == "[CREDENTIAL REDACTED: k]"
    assert cp.redact_credentials("KEY=medx_123456") == "KEY=[CREDENTIAL REDACTED: k]"


def test_env_path_expands_environment_variables(tmp_path, monkeypatch):
    path = _write(tmp_path, {"patterns": [ORG_KEY_RE]})
    monkeypatch.setenv("MEDX_CFG_DIR", str(tmp_path))
    monkeypatch.setenv(ENV, os.path.join("$MEDX_CFG_DIR", path.name))
    cp.reset_custom_patterns_cache()
    assert ORG_KEY not in cp.redact_credentials(ORG_KEY)


def test_placeholder_anchor_between_segments_still_redacts(tmp_path, monkeypatch):
    """A secret that directly follows a built-in placeholder is still caught."""
    _use(monkeypatch, _write(tmp_path, {"patterns": [ORG_KEY_RE]}))
    out = cp.redact_credentials(f"{AWS_KEY}{ORG_KEY}")
    assert ORG_KEY not in out
    assert out == "[CREDENTIAL REDACTED: AWS access key][CREDENTIAL REDACTED: custom pattern]"
