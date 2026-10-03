"""The Claude Code plugin under ``plugin/`` and the marketplace at the repository root."""

import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

import pytest
import typer.main

import grepogram
from grepogram.cli import app

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


# --- drift check: what the skills and commands tell Claude to run must exist in the CLI ---

NEGATIVE_NUMBER = re.compile(r"-\d+(\.\d+)?")
ALLOWED_TOOL = re.compile(r"Bash\((grepogram[^:()]*?)(?::\*)?\)")
INLINE_SPAN = re.compile(r"`([^`\n]+)`")
FLOOR = re.compile(r"grepogram >= (\d+\.\d+\.\d+)")
POSITIONAL_READERS = {"thread", "context"}


def markdown_files() -> list[Path]:
    return sorted(
        [*(PLUGIN_DIR / "commands").glob("*.md"), *(PLUGIN_DIR / "skills").glob("*/SKILL.md")]
    )


def command_lines(text: str) -> list[tuple[int, str]]:
    """Every ``grepogram ...`` command a markdown file spells out, with its 1-based line number:
    ``allowed-tools`` entries, fenced-block lines and inline code spans."""
    found: list[tuple[int, str]] = []
    lines = text.splitlines()
    in_front = bool(lines) and lines[0].strip() == "---"
    in_fence = False
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if in_front:
            if number > 1 and stripped == "---":
                in_front = False
            else:
                found.extend((number, m.group(1).strip()) for m in ALLOWED_TOOL.finditer(line))
            continue
        if stripped.startswith("```"):
            in_fence = not in_fence
        elif in_fence:
            if stripped.startswith("grepogram "):
                found.append((number, stripped))
        else:
            found.extend(
                (number, m.group(1).strip())
                for m in INLINE_SPAN.finditer(line)
                if m.group(1).strip().startswith("grepogram ")
            )
    return found


def check_command(root: Any, command: str) -> str | None:
    """``None`` when ``command`` parses against the CLI tree, else what is wrong with it."""
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        return f"cannot split {command!r}: {exc}"
    assert tokens[0] == "grepogram"
    rest = tokens[1:]
    node: Any = root
    path: list[str] = []
    index = 0

    def known(cmd: Any) -> set[str]:
        opts = {opt for p in cmd.params for opt in (*p.opts, *p.secondary_opts)}
        return opts | {"--help"}

    while index < len(rest) and hasattr(node, "commands"):
        token = rest[index]
        if token.startswith("-"):
            if token.split("=", 1)[0] not in known(node):
                return f"unknown option {token} for {' '.join(['grepogram', *path])}"
            index += 1
            continue
        child = node.commands.get(token)
        if child is None:
            return f"no subcommand {token!r} under {' '.join(['grepogram', *path])}"
        node = child
        path.append(token)
        index += 1
    options = known(node)
    tail = rest[index:]
    before = tail[: tail.index("--")] if "--" in tail else tail
    for token in before:
        if NEGATIVE_NUMBER.fullmatch(token):
            return f"{token} before -- reads as an option; put -- first"
        if token.startswith("-") and token.split("=", 1)[0] not in options:
            return f"unknown option {token} for {' '.join(['grepogram', *path])}"
    if (
        path
        and path[-1] in POSITIONAL_READERS
        and "--" not in tail
        and any(not t.startswith("-") for t in tail)
    ):
        return f"positional ids of {path[-1]} must follow --"
    return None


def drift(root: Any, name: str, text: str) -> list[str]:
    problems: list[str] = []
    for number, command in command_lines(text):
        problem = check_command(root, command)
        if problem:
            problems.append(f"{name}:{number}: {problem}: {command}")
    return problems


def test_skills_and_commands_only_name_what_the_cli_has() -> None:
    root = typer.main.get_command(app)
    files = markdown_files()
    assert files
    problems: list[str] = []
    for path in files:
        problems += drift(root, str(path.relative_to(ROOT)), path.read_text(encoding="utf-8"))
    assert not problems, "\n".join(problems)


def synthetic(*lines: str) -> str:
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ("`grepogram frobnicate now`", "no subcommand"),
        ("`grepogram sources frob`", "no subcommand"),
        ("`grepogram search --frob x`", "unknown option --frob"),
        ("`grepogram thread --json -1001 5`", "before --"),
        ("`grepogram thread --json 5 7`", "must follow --"),
        ("`grepogram search 'unbalanced`", "cannot split"),
    ],
)
def test_drift_flags_bad_commands(body: str, fragment: str) -> None:
    problems = drift(typer.main.get_command(app), "x.md", synthetic("text", body))
    assert len(problems) == 1
    assert problems[0].startswith("x.md:2:")
    assert fragment in problems[0]


@pytest.mark.parametrize(
    "body",
    [
        "`grepogram thread --json -- -1001234567 5`",
        "`grepogram context --json --before 3 -- -1001234567 5`",
        "`grepogram search --json --chat=-1001234567 <query>`",
        "`grepogram search --no-rerank -k 5 -c x -a work <query>`",
        "`grepogram --version`",
        "`grepogram sources ls`",
        "`grepogram dialogs -n 5 -a work <query>`",
        "`grepogram search --since 7d \u2026`",
        "`ls` and `uv tool upgrade grepogram` are not grepogram commands",
    ],
)
def test_drift_accepts_good_commands(body: str) -> None:
    assert drift(typer.main.get_command(app), "x.md", synthetic(body)) == []


def test_drift_reads_fenced_blocks() -> None:
    text = synthetic("```bash", "grepogram search --frob x", "```")
    assert len(drift(typer.main.get_command(app), "x.md", text)) == 1


def test_drift_checks_allowed_tools_in_list_and_comma_form() -> None:
    root = typer.main.get_command(app)
    comma = synthetic(
        "---", "allowed-tools: Bash(grepogram search:*), Bash(grepogram nope:*)", "---"
    )
    listed = synthetic(
        "---", "allowed-tools:", "  - Bash(grepogram search:*)", "  - Bash(grepogram nope:*)", "---"
    )
    for text in (comma, listed):
        problems = drift(root, "x.md", text)
        assert len(problems) == 1
        assert "nope" in problems[0]
    good = synthetic(
        "---", "allowed-tools: Bash(grepogram --version:*), Bash(grepogram sources ls:*)", "---"
    )
    assert drift(root, "x.md", good) == []


def test_drift_names_file_and_line_of_a_split_error() -> None:
    text = synthetic("a", "b", '`grepogram search "x`')
    problems = drift(typer.main.get_command(app), "skills/x.md", text)
    assert problems and problems[0].startswith("skills/x.md:3: cannot split")


# --- floor check: one stated minimum CLI version, the same everywhere, never ahead of us ---


def version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def test_cli_floor_is_stated_once_per_file_and_agrees() -> None:
    floors: dict[str, str] = {}
    for path in markdown_files():
        found = FLOOR.findall(path.read_text(encoding="utf-8"))
        assert len(found) == 1, f"{path.relative_to(ROOT)} states the floor {len(found)} times"
        floors[str(path.relative_to(ROOT))] = found[0]
    assert len(set(floors.values())) == 1, floors
    floor = next(iter(floors.values()))
    assert version_tuple(floor) <= version_tuple(grepogram.__version__)


def test_plugin_text_has_no_absolute_paths_or_image_names() -> None:
    for path in PLUGIN_DIR.rglob("*"):
        if not path.is_file() or path.suffix in {".png", ".jpg", ".jpeg", ".svg"}:
            continue
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"/(Users|home)/\w+", text.replace("/Users/x/", "")), path
        assert not re.search(r"\.(png|jpe?g|svg|ico)\b", text, re.IGNORECASE), path


def test_privacy_policy_exists_and_the_manifest_names_it() -> None:
    url = load(MANIFEST)["privacyPolicyUrl"]
    assert url.startswith(load(MANIFEST)["repository"])
    assert url.rsplit("/", 1)[-1] == "PRIVACY.md"
    text = (ROOT / "PRIVACY.md").read_text(encoding="utf-8")
    assert "huggingface.co" in text
    assert "telemetry" in text
    assert not re.search(r"\.(png|jpe?g|svg|ico)\b", text, re.IGNORECASE)
