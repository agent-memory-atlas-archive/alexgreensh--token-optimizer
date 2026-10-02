#!/usr/bin/env python3
"""Packaging guards for the token-optimizer-desktop mod plugin.

The desktop status bar ships as its own plugin (desktop/token-optimizer-desktop)
so that installing it, not installing it, or an older Claude Code that cannot
load function-hook modules never touches the main plugin's classic hooks. These
tests pin the four facts that keep it separate:

  * the marketplace lists it, from a source folder that exists, at the version
    its own manifest carries;
  * it versions on its own: never the root plugin's version, and the release
    bump script leaves its entry alone;
  * the main plugin's hooks.json (and its mirrors) never grows a ``modules``
    key, which an older Claude Code could reject along with every hook;
  * its manifest declares the two settings (enabled, animate) and the types
    contract, and its hooks.json names a module that exists.

Run: python3 -m pytest tests/test_desktop_plugin_packaging.py -q
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MARKETPLACE = REPO / ".claude-plugin" / "marketplace.json"
ROOT_MANIFEST = REPO / ".claude-plugin" / "plugin.json"
DESKTOP_NAME = "token-optimizer-desktop"
DESKTOP_DIR = REPO / "desktop" / DESKTOP_NAME
DESKTOP_MANIFEST = DESKTOP_DIR / ".claude-plugin" / "plugin.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _desktop_entry(marketplace: Path = MARKETPLACE) -> dict:
    entries = [p for p in _load(marketplace)["plugins"] if p["name"] == DESKTOP_NAME]
    assert len(entries) == 1, f"expected one {DESKTOP_NAME} marketplace entry, got {len(entries)}"
    return entries[0]


def test_marketplace_lists_desktop_plugin_from_existing_source():
    entry = _desktop_entry()
    assert entry["source"] == "./desktop/token-optimizer-desktop"
    source = (REPO / entry["source"]).resolve()
    assert source == DESKTOP_DIR.resolve()
    assert DESKTOP_MANIFEST.is_file(), f"{DESKTOP_MANIFEST} missing"
    assert entry["category"] == "productivity"
    assert entry["license"] == _load(ROOT_MANIFEST)["license"]


def test_marketplace_version_matches_desktop_manifest():
    manifest = _load(DESKTOP_MANIFEST)
    assert manifest["name"] == DESKTOP_NAME
    assert _desktop_entry()["version"] == manifest["version"]


def test_desktop_version_never_equals_root_version():
    # Equal versions would let bump_patch_version.py's "version": "<root>"
    # rewrite catch the desktop entry and silently re-version it.
    root_version = _load(ROOT_MANIFEST)["version"]
    assert _load(DESKTOP_MANIFEST)["version"] != root_version
    assert _desktop_entry()["version"] != root_version


def test_main_plugin_hooks_json_has_no_modules_key():
    # The Codex and Cowork copies are generated from the root file; checking
    # them too catches a module key that slipped in through a mirror.
    for hooks_json in (
        REPO / "hooks" / "hooks.json",
        REPO / "plugins" / "token-optimizer" / "hooks" / "hooks.json",
        REPO / "cowork" / "token-optimizer" / "hooks" / "hooks.json",
    ):
        if not hooks_json.exists():
            continue
        assert "modules" not in _load(hooks_json), f"{hooks_json.relative_to(REPO)} has a modules key"


def test_desktop_manifest_declares_settings_and_types_contract():
    manifest = _load(DESKTOP_MANIFEST)
    assert manifest["license"] == _load(ROOT_MANIFEST)["license"]
    assert manifest["types"] == "./types/index.d.ts"
    assert (DESKTOP_DIR / "types" / "index.d.ts").is_file()
    config = manifest["userConfig"]
    for field in ("enabled", "animate"):
        assert config[field]["type"] == "boolean"
        assert config[field]["default"] is True


def test_desktop_hooks_json_names_an_existing_module_and_no_classic_hooks():
    hooks = _load(DESKTOP_DIR / "hooks" / "hooks.json")
    assert hooks == {"modules": ["./register.tsx"]}
    assert (DESKTOP_DIR / "hooks" / "register.tsx").is_file()


def test_bump_script_does_not_list_desktop_entry():
    sys.path.insert(0, str(REPO / "scripts"))
    try:
        import bump_patch_version  # noqa: PLC0415
    finally:
        sys.path.pop(0)
    assert DESKTOP_NAME not in bump_patch_version.MARKETPLACE_ENTRIES


def _copy_bump_inputs(tmp_path: Path) -> None:
    for relative in (
        ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json",
        ".codex-plugin/plugin.json", "opencode/src/dashboard/generator.ts",
        "openclaw/src/dashboard.ts", "scripts/bump_patch_version.py",
    ):
        dest = tmp_path / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / relative, dest)
    if (REPO / "package.json").exists():
        shutil.copyfile(REPO / "package.json", tmp_path / "package.json")


def test_patch_bump_leaves_desktop_entry_alone(tmp_path):
    _copy_bump_inputs(tmp_path)
    before = _desktop_entry(tmp_path / ".claude-plugin" / "marketplace.json")
    old_root = _load(tmp_path / ".claude-plugin" / "plugin.json")["version"]
    bumped = subprocess.run(
        [sys.executable, str(tmp_path / "scripts" / "bump_patch_version.py")],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert bumped != old_root
    market = _load(tmp_path / ".claude-plugin" / "marketplace.json")
    by_name = {p["name"]: p for p in market["plugins"]}
    assert by_name["token-optimizer"]["version"] == bumped
    assert by_name[DESKTOP_NAME] == before
