"""The Claude Code plugin under ``plugin/`` and the marketplace at the repository root."""

import dataclasses
import json
import os
import re
import shlex
import struct
import subprocess
from pathlib import Path
from typing import Any

import pytest
import typer.main
from packaging.version import Version

import grepogram
from grepogram import cli
from grepogram.cli import app
from grepogram.models import (
    Candidate,
    Evidence,
    Hit,
    MessageView,
    ResearchCfg,
    SearchCfg,
    SearchResult,
)

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
    assert entry["description"] == load(MANIFEST)["description"]


def test_plugin_bundles_no_mcp_server_and_no_executables() -> None:
    assert not (PLUGIN_DIR / ".mcp.json").exists()
    assert "mcpServers" not in load(MANIFEST)
    assert not (PLUGIN_DIR / "bin").exists()


def test_release_refuses_a_plugin_version_mismatch() -> None:
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "jq -r .version plugin/.claude-plugin/plugin.json" in release


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
        "grepogram research \\\n  approve 3 1 --json --confirm abc",
        "grepogram accounts \\\n  rm work --json --confirm abc",
        "grepogram research\tapprove 3 1 --json --confirm abc",
        "grepogram accounts\trm work --confirm abc",
        "grepogram research 'approve' 3 1 --json --confirm abc",
        'grepogram research "approve" 3 1 --json --confirm abc',
        "grepogram 'accounts' rm work --confirm abc",
        "grepogram leave '--confirm' abc -- @chat",
        "GREPOGRAM research approve 3 1 --json --confirm abc",
        "Grepogram Accounts RM work --Confirm abc",
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


def test_the_one_hook_runs_the_gate_on_every_bash_call() -> None:
    hooks = load(HOOKS)["hooks"]
    assert list(hooks) == ["PreToolUse"]
    [entry] = hooks["PreToolUse"]
    assert entry["matcher"] == "Bash"
    [hook] = entry["hooks"]
    assert hook["type"] == "command"
    assert hook["command"] == '"${CLAUDE_PLUGIN_ROOT}/scripts/consent-gate.sh"'
    assert GATE.is_file()
    assert os.access(GATE, os.X_OK)


def test_gate_script_is_self_contained() -> None:
    lines = GATE.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "#!/bin/bash"
    code = [line for line in lines if not line.lstrip().startswith("#")]
    for line in code:
        assert not re.search(r"(^|[;&|(]|then|do)\s*(source|\.)\s", line), line
        assert "<<" not in line, line
        assert "CLAUDE_PLUGIN_ROOT" not in line, line
        assert not re.search(r"\b(uvx|npx|pip|npm|brew|jq|curl|wget|bash|sh)\s", line), line


# --- drift check: what the skills and commands tell Claude to run must exist in the CLI ---

NEGATIVE_NUMBER = re.compile(r"-\d+(\.\d+)?")
ALLOWED_TOOL = re.compile(r"Bash\((grepogram[^:()]*?)(?::\*)?\)")
INLINE_SPAN = re.compile(r"`([^`\n]+)`")
FENCES = ("```", "~~~")


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
        if stripped.startswith(FENCES):
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


def options_of(cmd: Any) -> dict[str, Any]:
    return {
        opt: p
        for p in cmd.params
        if p.param_type_name == "option"
        for opt in (*p.opts, *p.secondary_opts)
    }


def all_options(node: Any) -> set[str]:
    found = set(options_of(node)) | {"--help"}
    for child in getattr(node, "commands", {}).values():
        found |= all_options(child)
    return found


def needs_dashdash(cmd: Any, path: list[str]) -> bool:
    """Whether ``cmd`` takes free text or a chat as a positional value (a string argument): such a
    value can start with ``-`` (a channel id, a query), so it goes after ``--``. ``research
    approve`` is the exception: its items are candidate ids and grant names, never dash-led, and
    its confirming call appends ``--confirm`` at the end."""
    return path != ["research", "approve"] and any(
        p.param_type_name == "argument" and p.type.name == "str" for p in cmd.params
    )


def check_command(root: Any, command: str) -> str | None:
    """``None`` when ``command`` parses against the CLI tree, else what is wrong with it. Square
    brackets mark optional parts and are dropped; an option's value is skipped, so it may start
    with ``-``."""
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        return f"cannot split {command!r}: {exc}"
    assert tokens[0] == "grepogram"
    rest = [token.strip("[]") for token in tokens[1:]]
    node: Any = root
    path: list[str] = []
    positional: list[str] = []
    while rest:
        token = rest.pop(0)
        here = " ".join(["grepogram", *path])
        if token == "--":
            break
        if NEGATIVE_NUMBER.fullmatch(token):
            return f"{token} before -- reads as an option; put -- first"
        if token.startswith("-"):
            name, has_value, _ = token.partition("=")
            if name == "--help":
                continue
            param = options_of(node).get(name)
            if param is None:
                return f"unknown option {name} for {here}"
            takes_value = not (param.is_flag or param.count)
            if has_value and not takes_value:
                return f"{name} of {here} takes no value"
            if takes_value and not has_value and rest:
                rest.pop(0)
            continue
        if hasattr(node, "commands"):
            child = node.commands.get(token)
            if child is None:
                return f"no subcommand {token!r} under {here}"
            node = child
            path.append(token)
        else:
            positional.append(token)
    if positional and needs_dashdash(node, path):
        return f"positional values of {' '.join(['grepogram', *path])} must follow --"
    return None


def drift(root: Any, name: str, text: str) -> list[str]:
    """What is wrong with the commands ``text`` spells out, and with the options it names on
    their own in an inline span (`--full`, `-k 20`), which must exist on some command."""
    problems: list[str] = []
    for number, command in command_lines(text):
        problem = check_command(root, command)
        if problem:
            problems.append(f"{name}:{number}: {problem}: {command}")
    known = all_options(root)
    for number, line in enumerate(text.splitlines(), start=1):
        for match in INLINE_SPAN.finditer(line):
            span = match.group(1).strip()
            word = span.split(maxsplit=1)[0].split("=")[0] if span else ""
            if word.startswith("-") and word not in {"-", "--"} and not NEGATIVE_NUMBER.match(word):
                if word not in known:
                    problems.append(f"{name}:{number}: unknown option {word}: {span}")
    return problems


def test_skills_and_commands_only_name_what_the_cli_has() -> None:
    root = typer.main.get_command(app)
    files = markdown_files()
    assert files
    problems: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert command_lines(text), f"{path.relative_to(ROOT)} names no grepogram command"
        problems += drift(root, str(path.relative_to(ROOT)), text)
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
        ("`grepogram search --json <query>`", "must follow --"),
        ("`grepogram sources add <chat_id>`", "must follow --"),
        ("`grepogram leave --json <chat>`", "must follow --"),
        ("`grepogram research exclude <chat>`", "must follow --"),
        ("`grepogram research exclude [--reasonx <text>] -- <chat>`", "unknown option --reasonx"),
        ("`grepogram search --json=yes -- x`", "takes no value"),
        ("`--frob` on its own", "unknown option --frob"),
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
        "`grepogram search --json --chat=-1001234567 -- <query>`",
        "`grepogram search --json --chat -1001234567 -- <query>`",
        "`grepogram search --no-rerank -k 5 -c x -a work -- <query>`",
        "`grepogram --version`",
        "`grepogram sources ls`",
        "`grepogram dialogs -n 5 -a work -- <query>`",
        "`grepogram search --since 7d -- …`",
        "`grepogram research exclude [--reason <text>] -- <chat>...`",
        "`grepogram research approve --json <session_id> <items>`",
        "`grepogram research status --json [<session_id>]`",
        "`--full`, `-k 20`, `--seed` / `-s`, `-`, `--` and `-100...` on their own",
        "`ls` and `uv tool upgrade grepogram` are not grepogram commands",
    ],
)
def test_drift_accepts_good_commands(body: str) -> None:
    assert drift(typer.main.get_command(app), "x.md", synthetic(body)) == []


def test_drift_reads_fenced_blocks() -> None:
    for fence in FENCES:
        text = synthetic(f"{fence}bash", "grepogram search --frob x", fence)
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


# --- allowed-tools: what runs without a prompt must change nothing -------------------------

# Reading the index, the account's chat list and research state, plus the local sync and the
# research steps that only add or narrow candidates. Never an approval, a run, an exclusion, a
# source, an account, the config, the install or the MCP registration.
READ_ONLY = {
    "--version",
    "search",
    "thread",
    "context",
    "sync",
    "dialogs",
    "sources ls",
    "accounts ls",
    "config path",
    "research start",
    "research discover",
    "research candidates",
    "research status",
    "research skip",
    "research stop",
}


def allowed_tools(text: str) -> list[str]:
    """The ``allowed-tools`` entries of a file's frontmatter, in comma or list form."""
    assert text.startswith("---\n"), "the frontmatter must open on the first line"
    front = text[4:].split("\n---\n", 1)[0]
    match = re.search(r"^allowed-tools:(.*?)(?=^\S|\Z)", front, re.MULTILINE | re.DOTALL)
    if match is None:
        return []
    entries = (part.strip(" -\t") for part in re.split(r"[,\n]", match.group(1)))
    return [entry for entry in entries if entry]


def unsafe_tools(text: str) -> list[str]:
    def safe(entry: str) -> bool:
        match = re.fullmatch(r"Bash\(grepogram ([^:()]+):\*\)", entry)
        return match is not None and match.group(1) in READ_ONLY

    return [entry for entry in allowed_tools(text) if not safe(entry)]


def test_allowed_tools_pre_allow_only_commands_that_change_nothing() -> None:
    for path in markdown_files():
        text = path.read_text(encoding="utf-8")
        assert allowed_tools(text), path.relative_to(ROOT)
        assert unsafe_tools(text) == [], path.relative_to(ROOT)


@pytest.mark.parametrize(
    "entry",
    [
        "Bash(grepogram:*)",
        "Bash(grepogram research:*)",
        "Bash(grepogram research approve:*)",
        "Bash(grepogram research run:*)",
        "Bash(grepogram research exclude:*)",
        "Bash(grepogram accounts rm:*)",
        "Bash(grepogram leave:*)",
        "Bash(grepogram sources add:*)",
        "Bash(grepogram config init:*)",
        "Bash(grepogram auth:*)",
        "Bash(uv tool install:*)",
        "Bash(claude mcp add:*)",
        "Read",
    ],
)
def test_an_allowed_tool_that_can_change_something_is_refused(entry: str) -> None:
    for text in (
        synthetic("---", f"allowed-tools: Bash(grepogram search:*), {entry}", "---"),
        synthetic("---", "allowed-tools:", "  - Bash(grepogram search:*)", f"  - {entry}", "---"),
    ):
        assert unsafe_tools(text) == [entry]


def test_frontmatter_hidden_behind_a_bom_is_refused() -> None:
    with pytest.raises(AssertionError):
        allowed_tools("﻿" + synthetic("---", "allowed-tools: Bash(grepogram leave:*)", "---"))


# --- the facts the skills restate from the code -----------------------------------------------


def test_skill_facts_match_the_code() -> None:
    search_skill = (PLUGIN_DIR / "skills" / "search" / "SKILL.md").read_text(encoding="utf-8")
    research_skill = (PLUGIN_DIR / "skills" / "research" / "SKILL.md").read_text(encoding="utf-8")
    assert cli.CONFIRM_EXIT == 3
    assert "exits with code 3" in research_skill
    assert SearchCfg().auto_sync_after_min == 60
    assert "Above 60 minutes" in search_skill
    assert ResearchCfg().run_budget_s == 300
    assert "5 minutes by default" in research_skill

    def names(*types: Any) -> set[str]:
        return {f.name for t in types for f in dataclasses.fields(t)}

    hit_names = names(SearchResult, Hit, MessageView)
    for name in ("index_age_min", "warnings", "url", "snippet", "chat", "anchor_msg_id"):
        assert name in hit_names and f"`{name}`" in search_skill, name
    for name in ("date_start", "date_end", "accounts", "chat_id", "msg_id"):
        assert name in hit_names and f"`{name}`" in search_skill, name
    candidate_names = names(Candidate, Evidence)
    for name in ("title", "identity", "status", "member", "via", "snippet", "msg_id"):
        assert name in candidate_names and f"`{name}`" in research_skill, name


# --- floor check: one stated minimum CLI version, the same everywhere, never ahead of us ---

# Loose on purpose, so a second floor written another way is counted too.
FLOOR = re.compile(r"grepogram\s*(?:>=|≥)\s*v?(\d[\w.]*\w)")


def test_cli_floor_is_stated_once_per_file_and_agrees() -> None:
    floors: dict[str, str] = {}
    for path in markdown_files():
        name = str(path.relative_to(ROOT))
        text = path.read_text(encoding="utf-8")
        found = FLOOR.findall(text)
        assert len(found) == 1, f"{name} states the floor {len(found)} times"
        assert f"grepogram >= {found[0]}" in text, f"{name} spells the floor another way"
        floors[name] = found[0]
    assert len(set(floors.values())) == 1, floors
    floor = next(iter(floors.values()))
    assert Version(floor) <= Version(grepogram.__version__)


# --- what the plugin ships ----------------------------------------------------------------------


def test_plugin_text_has_no_absolute_paths_image_names_or_installers() -> None:
    for path in PLUGIN_DIR.rglob("*"):
        raw = path.read_bytes() if path.is_file() else b"\0"
        if b"\0" in raw:
            continue
        text = raw.decode("utf-8")
        assert not re.search(r"/(Users|home|opt|usr/local)/", text), path
        assert not re.search(r"\.(png|jpe?g|svg|ico|gif|webp)\b", text, re.IGNORECASE), path
        assert not re.search(r"`(npx|uvx|pip3?|npm|brew|curl|wget)\s", text), path


def test_skill_names_match_their_directories_and_setup_is_user_only() -> None:
    for skill in (PLUGIN_DIR / "skills").glob("*/SKILL.md"):
        text = skill.read_text(encoding="utf-8")
        assert re.search(rf"^name: {skill.parent.name}$", text, re.MULTILINE), skill
    setup = (PLUGIN_DIR / "commands" / "setup.md").read_text(encoding="utf-8")
    assert re.search(r"^disable-model-invocation: true$", setup, re.MULTILINE)


def test_privacy_policy_exists_and_the_manifest_names_it() -> None:
    manifest = load(MANIFEST)
    assert manifest["privacyPolicyUrl"] == f"{manifest['repository']}/blob/main/PRIVACY.md"
    assert (ROOT / "PRIVACY.md").is_file()


ICON_DIR = PLUGIN_DIR / ".claude-plugin"
# The directories the project ships or documents, beside its top-level files. Never a tool's
# cache, a virtualenv or a local archive such as `.revmux/` or `.claude/settings.local.json`.
SCANNED_DIRS = (
    "plugin",
    "docs",
    "grepogram",
    "tests",
    ".github",
    ".claude-plugin",
    ".claude/rules",
)


def project_files() -> list[Path]:
    files = [path for path in ROOT.iterdir() if path.is_file()]
    for name in SCANNED_DIRS:
        files += [path for path in (ROOT / name).rglob("*") if path.is_file()]
    return files


def test_icon_is_one_square_png_of_a_sane_size() -> None:
    icons = list(ICON_DIR.glob("*.png"))
    assert len(icons) == 1
    data = icons[0].read_bytes()
    assert len(data) < 2 * 1024 * 1024
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    length, chunk = struct.unpack(">I4s", data[8:16])
    assert (length, chunk) == (13, b"IHDR")
    width, height = struct.unpack(">II", data[16:24])
    assert width == height
    assert 512 <= width <= 2048


def test_no_project_text_file_names_the_icon() -> None:
    name = next(ICON_DIR.glob("*.png")).name.encode()
    offenders: list[str] = []
    for path in project_files():
        raw = path.read_bytes()
        if b"\0" not in raw and name in raw:
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
