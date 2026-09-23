---
title: User-defined redaction patterns must not touch placeholders or be read as templates
category: security-issues
component: token-optimizer-credential-redaction
runtime: all
severity: medium
verified: true
---

# User-defined redaction patterns: placeholder and template safety

## Problem

`credential_patterns.redact_credentials` gained user-defined patterns loaded
from `redact-patterns.json` / `TOKEN_OPTIMIZER_REDACT_PATTERNS_FILE`. Built-in
patterns are written by us and are safe to run in sequence on the whole text.
User patterns are arbitrary regexes and labels, and three things that are fine
for built-ins break for them.

## What goes wrong, and the fix

1. **Broad user regexes match inside placeholders.** A pattern like
   `[A-Z]{5,}` matches `REDACTED` inside `[CREDENTIAL REDACTED: AWS access key]`
   and nests or corrupts the marker. Reusing the M-16 sentinel does not help:
   the sentinel (`\x00\x01REDACTED\x00\x01`) contains letters a user regex can
   match, and sentinels are restored by position (`str.replace(..., 1)`), so a
   second protection pass would restore labels in the wrong order.
   **Fix:** run each custom pattern only on the text between `_PLACEHOLDER_RE`
   matches (`_redact_custom`), then protect all placeholders — the pre-existing
   ones and the ones the custom patterns just inserted — before the built-ins
   run. Placeholders are never part of any string a regex sees. Custom patterns
   run first so an org pattern can claim a composite secret
   (`MEDX-123456-<jwt>`) whole instead of leaving a readable id beside a
   built-in placeholder.

2. **Labels are user text.** `pat.sub(f"[CREDENTIAL REDACTED: {label}]", text)`
   treats `\1` or `\g<0>` in a label as a backreference and can raise or leak
   the matched secret back into the output. **Fix:** always substitute with a
   function (`_sub_with_placeholder`), never a template string. Labels are also
   stripped of `[`, `]` and control characters so `_PLACEHOLDER_RE` still
   recognizes the placeholder on the next pass.

3. **Zero-width matches.** `\b` or a lookahead passes a "does it match empty
   text" check but still produces zero-length matches in real text, which would
   put a placeholder at every word boundary. **Fix:** the replacement function
   returns zero-length matches unchanged, and a `keep` group that covers the
   whole match redacts nothing.

## Guardrails that stay

- `CREDENTIAL_PATTERNS` and `PATTERNS_ONLY` remain the built-in set. They are
  import-time constants that the compressors use to decide which lines to keep
  verbatim, and the security report counts them. Custom patterns are exposed
  through `get_custom_patterns()` and `custom_patterns_status()`.
- Loading is lazy and cached per process. No file I/O at import, because
  `credential_patterns` is imported on every hook's hot path and
  `build_output_compress` fails closed if the import fails.
- `scripts/check_docs_claims.py` treats "N patterns" in docs as a countable
  claim. Describe the custom limit as "200 entries", not "200 patterns".

## Verification

`tests/test_custom_redaction_patterns.py` covers each case above with a
negative control (the same input without the custom file keeps the value).
