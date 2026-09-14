"""The release-installability gate must actually go red.

Between v5.11.57 and v5.11.64, eight consecutive releases shipped with no
CHECKSUMS.sha256 asset. install.sh verifies every install against that asset and
verification is the default path (install.sh:787); resolve_latest_release()
fails when the asset is absent (install.sh:815) and the caller turns that into a
hard abort (install.sh:962). So for eight releases the documented `curl | bash`
install aborted for every new user, while the repo looked healthy from the
inside: tests green, docs building, the maintainer's own machine current
(because /plugin installs from the marketplace clone and never crosses this
gate).

scripts/check-release-installable.sh is the guard for that class. Its entire
value is in its red path, and a red path nobody executes is a guess. So this
suite drives the script against local fixtures through its documented test seam
and asserts it fails, with a useful message, on every way the release can be
broken -- and passes on a well-formed one.
"""

import json
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE = REPO_ROOT / "scripts" / "check-release-installable.sh"

VALID_MANIFEST = (
    "0000000000000000000000000000000000000000000000000000000000000000  a.txt\n"
    "1111111111111111111111111111111111111111111111111111111111111111  b.txt\n"
)


def run_gate(api_json_path, extra_env=None):
    """Run the real script with its API endpoint pointed at a local fixture."""
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "RELEASES_API_URL_FOR_TESTS": f"file://{api_json_path}",
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(GATE)],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def write_release(tmp_path, name, assets, published_at=None):
    payload = {"tag_name": "v9.9.9", "assets": assets}
    if published_at is not None:
        payload["published_at"] = published_at
    p = tmp_path / name
    p.write_text(json.dumps(payload))
    return p


def fresh_release(tmp_path, name, assets):
    """A release published a moment ago: inside the signing grace window."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return write_release(tmp_path, name, assets, published_at=now)


def asset(url):
    return [{"name": "CHECKSUMS.sha256", "browser_download_url": f"file://{url}"}]


pytestmark = pytest.mark.skipif(
    sys.platform == "win32"
    or shutil.which("curl") is None
    or shutil.which("python3") is None,
    reason="POSIX-only bash gate harness requiring curl and python3",
)


def test_gate_fails_when_release_has_no_assets(tmp_path):
    """The exact v5.11.57-v5.11.64 state: a release with nothing attached."""
    api = write_release(tmp_path, "noassets.json", [])
    r = run_gate(api)
    assert r.returncode == 1
    assert "NOT INSTALLABLE" in r.stderr
    assert "RESULT: INSTALLABLE" not in r.stdout
    # The message must name the fix, not just the symptom.
    assert "sign-release.sh" in r.stderr
    assert "install.sh:962" in r.stderr


def test_gate_fails_when_release_has_other_assets_but_not_the_manifest(tmp_path):
    """A release with assets is not the same as a release with THE asset."""
    api = write_release(
        tmp_path,
        "otherassets.json",
        [{"name": "notes.txt", "browser_download_url": "file:///dev/null"}],
    )
    r = run_gate(api)
    assert r.returncode == 1
    assert "no CHECKSUMS.sha256 asset" in r.stderr
    assert "RESULT: INSTALLABLE" not in r.stdout
    # It should report what WAS attached, so the failure is diagnosable.
    assert "notes.txt" in r.stderr


def test_gate_fails_when_manifest_url_is_unreachable(tmp_path):
    """Advertised but undownloadable is still a broken install."""
    api = write_release(tmp_path, "badurl.json", asset(tmp_path / "missing.sha256"))
    r = run_gate(api)
    assert r.returncode == 1
    assert "could not be downloaded" in r.stderr
    assert "RESULT: INSTALLABLE" not in r.stdout


def test_gate_fails_when_manifest_is_empty(tmp_path):
    empty = tmp_path / "empty.sha256"
    empty.write_text("")
    api = write_release(tmp_path, "emptyman.json", asset(empty))
    r = run_gate(api)
    assert r.returncode == 1
    assert "empty" in r.stderr
    assert "RESULT: INSTALLABLE" not in r.stdout


def test_gate_fails_when_manifest_is_malformed(tmp_path):
    """An HTML error page saved as the asset is the classic silent corruption:
    it passes a presence check and fails later, inside a user's install, as
    'your install may be compromised'."""
    junk = tmp_path / "junk.sha256"
    junk.write_text("<html>rate limited</html>\n")
    api = write_release(tmp_path, "malformed.json", asset(junk))
    r = run_gate(api)
    assert r.returncode == 1
    assert "malformed" in r.stderr
    assert "RESULT: INSTALLABLE" not in r.stdout


def test_gate_fails_when_api_returns_no_tag(tmp_path):
    p = tmp_path / "notag.json"
    p.write_text(json.dumps({"assets": []}))
    r = run_gate(p)
    assert r.returncode == 1
    assert "no tag_name" in r.stderr
    assert "RESULT: INSTALLABLE" not in r.stdout


def test_gate_passes_on_a_well_formed_release(tmp_path):
    """The green path, so a gate that fails everything is not mistaken for one
    that works."""
    manifest = tmp_path / "CHECKSUMS.sha256"
    manifest.write_text(VALID_MANIFEST)
    api = write_release(tmp_path, "good.json", asset(manifest))
    r = run_gate(api)
    assert r.returncode == 0, r.stderr
    assert "RESULT: INSTALLABLE" in r.stdout
    assert "manifest lines : 2" in r.stdout


def test_gate_accepts_a_manifest_whose_last_line_lacks_a_newline(tmp_path):
    """`wc -l` counts newlines, not lines, so a manifest with no trailing newline
    undercounts -- and a single-entry one reports 0, which must not be mistaken
    for empty. sign-release.sh writes trailing newlines, but the gate reads a
    file off the network and must not reject a valid manifest on a byte that
    carries no meaning."""
    manifest = tmp_path / "CHECKSUMS.sha256"
    manifest.write_text(
        "0000000000000000000000000000000000000000000000000000000000000000  a.txt"
    )  # no trailing \n
    api = write_release(tmp_path, "nonl.json", asset(manifest))
    r = run_gate(api)
    assert r.returncode == 0, r.stderr
    assert "RESULT: INSTALLABLE" in r.stdout


def test_gate_rejects_a_manifest_with_crlf_line_endings(tmp_path):
    """CRLF makes every path end in \\r, so sha256sum -c would look for
    'a.txt\\r' and fail inside a user's install as 'may be compromised'. Better
    to reject it here, loudly, than to ship it."""
    manifest = tmp_path / "CHECKSUMS.sha256"
    manifest.write_bytes(
        b"0000000000000000000000000000000000000000000000000000000000000000  a.txt\r\n"
    )
    api = write_release(tmp_path, "crlf.json", asset(manifest))
    r = run_gate(api)
    assert r.returncode == 1
    # A CRLF-specific message, not a generic "malformed": the fix is on the
    # signing machine's core.autocrlf, and the message should say so.
    assert "CRLF" in r.stderr
    assert "RESULT: INSTALLABLE" not in r.stdout


def test_gate_rejects_a_manifest_with_a_trailing_blank_line(tmp_path):
    """A blank line is not '<sha256>  <path>'. sha256sum tolerates it, but the
    gate is checking whether the asset is a well-formed manifest, and silently
    accepting junk lines is how a truncated download passes for real."""
    manifest = tmp_path / "CHECKSUMS.sha256"
    manifest.write_text(
        "0000000000000000000000000000000000000000000000000000000000000000  a.txt\n\n"
    )
    api = write_release(tmp_path, "blank.json", asset(manifest))
    r = run_gate(api)
    assert r.returncode == 1
    assert "malformed" in r.stderr


def test_gate_does_not_let_api_strings_inject_into_its_own_messages(tmp_path):
    """RELEASE_TAG and the asset names come off the network and land in printf
    and fail() strings. A crafted name containing a format specifier must not be
    interpreted as one."""
    api_path = tmp_path / "inject.json"
    api_path.write_text(
        json.dumps(
            {
                "tag_name": "v9.9.9-%s-%d",
                "assets": [{"name": "%s%s%n.txt", "browser_download_url": "file:///dev/null"}],
            }
        )
    )
    r = run_gate(api_path)
    assert r.returncode == 1
    # The literal text survives; no format expansion, no crash.
    assert "%s%s%n.txt" in r.stderr
    assert "RESULT: INSTALLABLE" not in r.stdout


def test_gate_waits_out_the_signing_window_then_still_fails(tmp_path):
    """A fresh release with no asset is usually just mid-signing (the Tests run
    races the Sign release workflow: v5.13.13 checked at publish+2s, the asset
    landed ~20s later). The gate polls through the grace window -- but if it
    closes with no asset, the release is broken and must still go red."""
    api = fresh_release(tmp_path, "fresh-noasset.json", [])
    r = run_gate(api, {
        "RELEASE_SIGNING_GRACE_SECONDS": "3",
        "RELEASE_SIGNING_POLL_SECONDS": "1",
    })
    assert r.returncode == 1
    assert "NOT INSTALLABLE" in r.stderr
    assert "sign-release.sh" in r.stderr


def test_gate_passes_when_the_asset_lands_mid_wait(tmp_path):
    """The race's happy path: publish is fresh, the manifest asset appears
    while the gate is polling, and the gate goes green instead of failing a
    release that was never broken."""
    api = fresh_release(tmp_path, "fresh-later.json", [])
    manifest = tmp_path / "CHECKSUMS.sha256"
    manifest.write_text(VALID_MANIFEST)

    def attach_asset():
        time.sleep(1.5)
        write_release(tmp_path, "fresh-later.json", asset(manifest),
                      published_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    t = threading.Thread(target=attach_asset)
    t.start()
    try:
        r = run_gate(api, {
            "RELEASE_SIGNING_GRACE_SECONDS": "30",
            "RELEASE_SIGNING_POLL_SECONDS": "1",
        })
    finally:
        t.join()
    assert r.returncode == 0, r.stderr
    assert "RESULT: INSTALLABLE" in r.stdout


def test_gate_does_not_wait_on_an_old_unsigned_release(tmp_path):
    """The grace window exists for the publish race only. A release older than
    the window with no asset is the v5.11.57-64 break and must fail at once,
    not burn CI minutes polling for a signing that already ended."""
    api = write_release(
        tmp_path, "old-noasset.json", [],
        published_at="2020-01-01T00:00:00Z",
    )
    start = time.monotonic()
    r = run_gate(api, {
        "RELEASE_SIGNING_GRACE_SECONDS": "600",
        "RELEASE_SIGNING_POLL_SECONDS": "1",
    })
    assert r.returncode == 1
    assert "NOT INSTALLABLE" in r.stderr
    assert time.monotonic() - start < 30


def test_gate_script_is_executable_and_syntactically_valid():
    assert GATE.exists(), f"{GATE} is missing"
    r = subprocess.run(["bash", "-n", str(GATE)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
