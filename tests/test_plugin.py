"""The Claude Code plugin under ``plugin/`` and the marketplace at the repository root."""

import json
from pathlib import Path
from typing import Any

import grepogram

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = ROOT / "plugin"
MANIFEST = PLUGIN_DIR / ".claude-plugin" / "plugin.json"
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"


def load(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_manifest_has_the_required_keys() -> None:
    manifest = load(MANIFEST)
    for key in ("name", "version", "description", "author", "license", "privacyPolicyUrl"):
        assert manifest.get(key), key
    assert manifest["name"] == "grepogram"


def test_manifest_version_matches_the_package() -> None:
    assert load(MANIFEST)["version"] == grepogram.__version__


def test_marketplace_lists_the_one_plugin_it_ships() -> None:
    marketplace = load(MARKETPLACE)
    assert marketplace["name"] == "grepogram"
    plugins = marketplace["plugins"]
    assert len(plugins) == 1
    entry = plugins[0]
    assert entry["name"] == load(MANIFEST)["name"]
    source = (ROOT / entry["source"]).resolve()
    assert source == PLUGIN_DIR.resolve()
    assert (source / ".claude-plugin" / "plugin.json").is_file()


def test_plugin_has_no_bundled_mcp_server() -> None:
    assert not (PLUGIN_DIR / ".mcp.json").exists()
    assert not (ROOT / ".mcp.json").exists()
