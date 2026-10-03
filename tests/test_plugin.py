"""The Claude Code plugin under ``plugin/`` and the marketplace at the repository root."""

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

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


HOOKS = PLUGIN_DIR / "hooks" / "hooks.json"
GATE = PLUGIN_DIR / "scripts" / "consent-gate.sh"


def run_gate(command: str | None, raw: str | None = None) -> subprocess.CompletedProcess[str]:
    payload = (
        raw
        if raw is not None
        else json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    )
    return subprocess.run(  # noqa: S603 - the script path is a repo constant
        [str(GATE)], input=payload, capture_output=True, text=True, check=False, timeout=10
    )


@pytest.mark.parametrize(
    "command",
    [
        "grepogram research approve 3 1:join --json --confirm abc",
        "grepogram research approve 3 1:join --json --confirm=abc",
        "/Users/x/.local/bin/grepogram accounts rm work --confirm abc",
        "uv run grepogram leave --confirm abc -- @chat",
        "grepogram accounts  rm work --confirm abc",
        "cd x && grepogram leave --json --confirm y -- -1001234567",
    ],
)
def test_gate_asks_on_a_confirming_call(command: str) -> None:
    result = run_gate(command)
    assert result.returncode == 0
    out = json.loads(result.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "ask"
    assert out["permissionDecisionReason"].startswith("grepogram:")


@pytest.mark.parametrize(
    "command",
    [
        "grepogram research approve 3 1:join --json",
        "grepogram accounts rm work",
        "grepogram leave --json -- @chat",
        "grepogram search --json 'hello'",
        "ls",
    ],
)
def test_gate_is_silent_without_a_confirm(command: str) -> None:
    result = run_gate(command)
    assert result.returncode == 0
    assert result.stdout == ""


def test_gate_is_silent_without_grepogram_or_payload() -> None:
    for raw in ("", "{}", json.dumps({"tool_input": {"command": "git leave --confirm x"}})):
        result = run_gate(None, raw=raw)
        assert result.returncode == 0
        assert result.stdout == ""


def test_gate_over_asks_on_a_search_query_naming_the_pattern() -> None:
    # Documented behaviour: the raw payload is matched, so a query containing the words asks.
    result = run_gate("grepogram search 'leave --confirm'")
    assert result.returncode == 0
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_hook_commands_point_at_an_executable_plugin_script() -> None:
    entries = load(HOOKS)["hooks"]["PreToolUse"]
    assert entries
    for entry in entries:
        for hook in entry["hooks"]:
            match = re.fullmatch(r'"\$\{CLAUDE_PLUGIN_ROOT\}/([^"$]+)"', hook["command"])
            assert match, hook["command"]
            script = PLUGIN_DIR / match.group(1)
            assert script.is_file()
            assert os.access(script, os.X_OK)


def test_gate_script_is_self_contained() -> None:
    lines = GATE.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "#!/bin/bash"
    code = [line for line in lines if not line.lstrip().startswith("#")]
    for line in code:
        assert not re.match(r"\s*(source|\.)\s", line), line
        assert "<<" not in line, line
        assert not re.search(r"\b(uvx|npx|pip|npm|brew)\s", line), line
