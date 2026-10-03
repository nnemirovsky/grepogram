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
import typer.core
import typer.main
from packaging.version import Version
from typer._click.core import Command

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
HOOKS = PLUGIN_DIR / "hooks" / "hooks.json"
GATE = PLUGIN_DIR / "scripts" / "consent-gate.sh"

# What the CLI tree is made of: typer vendors its own click, so the top-level `click` classes
# are not the ones in the tree.
_Node = Command
_CLI = typer.main.get_command(app)

# Tools that fetch and install; no plugin text may tell a user to run one, and the gate script
# (which must be self-contained) may use none of them nor `jq`, `bash` or `sh`.
_INSTALLERS = ("npx", "uvx", "pip", "pip3", "npm", "brew", "curl", "wget")


def _load(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _text_or_none(path: Path) -> str | None:
    """The text of a file, or ``None`` for a directory or a binary (NUL-holding) file."""
    if not path.is_file():
        return None
    raw = path.read_bytes()
    return None if b"\0" in raw else raw.decode("utf-8", errors="replace")


# --- manifest and marketplace ----------------------------------------------------------------


def test_manifest_has_the_required_keys() -> None:
    manifest = _load(MANIFEST)
    for key in ("name", "version", "description", "author", "license", "privacyPolicyUrl"):
        assert manifest.get(key), key
    assert manifest["name"] == "grepogram"


def test_manifest_version_matches_the_package() -> None:
    assert _load(MANIFEST)["version"] == grepogram.__version__


def test_marketplace_lists_the_one_plugin_it_ships() -> None:
    marketplace = _load(MARKETPLACE)
    assert marketplace["name"] == "grepogram"
    plugins = marketplace["plugins"]
    assert len(plugins) == 1
    entry = plugins[0]
    assert entry["name"] == _load(MANIFEST)["name"]
    source = (ROOT / entry["source"]).resolve()
    assert source == PLUGIN_DIR.resolve()
    assert entry["description"] == _load(MANIFEST)["description"]


def test_plugin_bundles_no_mcp_server_and_no_executables() -> None:
    assert not (PLUGIN_DIR / ".mcp.json").exists()
    assert "mcpServers" not in _load(MANIFEST)
    assert not (PLUGIN_DIR / "bin").exists()


def test_release_refuses_a_plugin_version_mismatch() -> None:
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "jq -r .version plugin/.claude-plugin/plugin.json" in release


# --- consent gate ----------------------------------------------------------------------------


def _bash_payload(command: str) -> str:
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})


def _run_gate(payload: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
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
        # the summary-printing first calls: their `command` field already holds --confirm
        "grepogram research approve 3 1:join --json",
        "grepogram accounts rm work",
        "grepogram leave --json -- @chat",
        "grepogram research approve --json 3 1 | jq -r .command | sh",
        'eval "$(grepogram research approve --json 3 1 | jq -r .command)"',
        "grepogram accounts rm work --json | jq -r .command | bash",
        "grepogram leave --json -- @chat | jq -r .command | bash",
        "grepogram research \\\n  approve 3 1 --json",
        "grepogram accounts \\\n  rm work --json",
        "grepogram research\tapprove 3 1 --json",
        "grepogram accounts\trm work",
        "grepogram research 'approve' 3 1 --json",
        'grepogram research "approve" 3 1 --json',
        "grepogram 'accounts' rm work",
        "Grepogram Accounts RM work",
        "grepogram -v leave -- @chat",
        "grepogram '--verbose' leave -- @chat",
        "grepogram \\\n  leave -- @chat",
        "grepogram\tLEAVE -- @chat",
        '"grepogram" leave -- @chat',
        "uv run grepogram leave -- -1001234567",
    ],
)
def test_gate_asks_on_every_consent_call(command: str) -> None:
    result = _run_gate(_bash_payload(command))
    assert result.returncode == 0
    out = json.loads(result.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "ask"
    assert out["permissionDecisionReason"].startswith("grepogram:")


@pytest.mark.parametrize(
    "command",
    [
        "grepogram search --json 'hello'",
        "grepogram search 'leave'",
        "grepogram search --json -- 'how to leave a group'",
        "grepogram research candidates --json 3",
        "grepogram accounts ls",
        "grepogram search 'accounts form'",
        "ls",
    ],
)
def test_gate_is_silent_on_other_calls(command: str) -> None:
    result = _run_gate(_bash_payload(command))
    assert result.returncode == 0
    assert result.stdout == ""


@pytest.mark.parametrize(
    "payload",
    ["", "{}", json.dumps({"tool_input": {"command": "git leave --confirm x"}})],
)
def test_gate_is_silent_without_grepogram_or_payload(payload: str) -> None:
    result = _run_gate(payload)
    assert result.returncode == 0
    assert result.stdout == ""


def test_gate_ignores_a_leave_outside_a_grepogram_call_in_a_grepogram_cwd() -> None:
    """The payload carries a ``cwd`` naming the repository: that alone, with the word ``leave``
    in some other command, must not ask."""
    payload = json.dumps(
        {
            "cwd": "/Users/x/grepogram",
            "tool_name": "Bash",
            "tool_input": {"command": "git commit -m 'leave the old path'"},
        }
    )
    result = _run_gate(payload)
    assert result.returncode == 0
    assert result.stdout == ""


@pytest.mark.parametrize(
    "command",
    [
        "grepogram search 'leave --confirm'",
        "grepogram search 'research approve'",
        "grepogram search 'accounts rm'",
    ],
)
def test_gate_over_asks_on_a_search_query_naming_the_pattern(command: str) -> None:
    """The chosen trade-off: anything may sit between grepogram and --confirm, `research approve`
    or `accounts rm`, so a query holding those asks; the two words of a command must be adjacent
    and `leave` must be the subcommand, so `accounts form` or a search for the word leave stays
    silent (tested in test_gate_is_silent_on_other_calls)."""
    result = _run_gate(_bash_payload(command))
    assert result.returncode == 0
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_the_one_hook_runs_the_gate_on_every_bash_call() -> None:
    hooks = _load(HOOKS)["hooks"]
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
    banned = "|".join((*_INSTALLERS, "jq", "bash", "sh"))
    code = [line for line in lines if not line.lstrip().startswith("#")]
    for line in code:
        assert not re.search(r"(^|[;&|(]|then|do)\s*(source|\.)\s", line), line
        assert "<<" not in line, line
        assert "CLAUDE_PLUGIN_ROOT" not in line, line
        assert not re.search(rf"\b({banned})\s", line), line


# --- drift check -----------------------------------------------------------------------------

# What the skills and commands tell Claude to run must exist in the CLI.

_NEGATIVE_NUMBER = re.compile(r"-\d+(\.\d+)?")
# One `allowed-tools` entry: group 1 is the command (`grepogram search`), group 2 the `:*` suffix.
_ALLOWED_TOOL = re.compile(r"Bash\((grepogram[^:()]*?)(:\*)?\)")
_INLINE_SPAN = re.compile(r"`([^`\n]+)`")
_FENCES = ("```", "~~~")


def _markdown_files() -> list[Path]:
    return sorted(
        [*(PLUGIN_DIR / "commands").glob("*.md"), *(PLUGIN_DIR / "skills").glob("*/SKILL.md")]
    )


def _frontmatter(text: str) -> tuple[list[tuple[int, str]], int]:
    """The frontmatter lines (1-based number, text) between the opening ``---`` on line 1 and the
    closing one, and the number of the first line after it. Empty and 1 when the text does not
    open with ``---`` on its first line."""
    if not text.startswith("---\n"):
        return [], 1
    lines = text.splitlines()
    end = next((n for n in range(1, len(lines)) if lines[n].strip() == "---"), len(lines))
    return [(n + 1, lines[n]) for n in range(1, end)], end + 2


def _command_lines(text: str) -> list[tuple[int, str]]:
    """Every ``grepogram ...`` command a markdown file spells out, with its 1-based line number:
    ``allowed-tools`` entries, fenced-block lines and inline code spans."""
    front, body_start = _frontmatter(text)
    found = [
        (number, m.group(1).strip()) for number, line in front for m in _ALLOWED_TOOL.finditer(line)
    ]
    in_fence = False
    for number, line in enumerate(text.splitlines(), start=1):
        if number < body_start:
            continue
        stripped = line.strip()
        if stripped.startswith(_FENCES):
            in_fence = not in_fence
        elif in_fence:
            if stripped.startswith("grepogram "):
                found.append((number, stripped))
        else:
            found.extend(
                (number, m.group(1).strip())
                for m in _INLINE_SPAN.finditer(line)
                if m.group(1).strip().startswith("grepogram ")
            )
    return found


def _options_of(cmd: _Node) -> dict[str, Any]:
    return {
        opt: p
        for p in cmd.params
        if p.param_type_name == "option"
        for opt in (*p.opts, *p.secondary_opts)
    }


def _all_options(node: _Node) -> set[str]:
    found = set(_options_of(node)) | {"--help"}
    if isinstance(node, typer.core.TyperGroup):
        for child in node.commands.values():
            found |= _all_options(child)
    return found


def _needs_dashdash(cmd: _Node, path: list[str]) -> bool:
    """Whether ``cmd`` takes free text or a chat as a positional value (a string argument): such a
    value can start with ``-`` (a channel id, a query), so it goes after ``--``. ``research
    approve`` is the exception: its items are candidate ids and grant names, never dash-led, and
    its confirming call appends ``--confirm`` at the end."""
    return path != ["research", "approve"] and any(
        p.param_type_name == "argument" and p.type.name == "str" for p in cmd.params
    )


def _check_command(root: _Node, command: str) -> str | None:
    """``None`` when ``command`` parses against the CLI tree, else what is wrong with it. Square
    brackets mark optional parts and are dropped; an option's value is skipped, so it may start
    with ``-``."""
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        return f"cannot split {command!r}: {exc}"
    if tokens[0] != "grepogram":
        return f"not a grepogram command: {command!r}"
    rest = [token.strip("[]") for token in tokens[1:]]
    node = root
    path: list[str] = []
    positional: list[str] = []
    while rest:
        token = rest.pop(0)
        here = " ".join(["grepogram", *path])
        if token == "--":
            break
        if _NEGATIVE_NUMBER.fullmatch(token):
            return f"{token} before -- reads as an option; put -- first"
        if token.startswith("-"):
            name, has_value, _ = token.partition("=")
            if name == "--help":
                continue
            param = _options_of(node).get(name)
            if param is None:
                return f"unknown option {name} for {here}"
            takes_value = not (param.is_flag or param.count)
            if has_value and not takes_value:
                return f"{name} of {here} takes no value"
            if takes_value and not has_value and rest:
                rest.pop(0)
            continue
        if isinstance(node, typer.core.TyperGroup):
            child = node.commands.get(token)
            if child is None:
                return f"no subcommand {token!r} under {here}"
            node = child
            path.append(token)
        else:
            positional.append(token)
    if positional and _needs_dashdash(node, path):
        return f"positional values of {' '.join(['grepogram', *path])} must follow --"
    return None


def _drift(root: _Node, name: str, text: str) -> list[str]:
    """What is wrong with the commands ``text`` spells out, and with the options it names on
    their own in an inline span (`--full`, `-k 20`), which must exist on some command."""
    problems: list[str] = []
    for number, command in _command_lines(text):
        problem = _check_command(root, command)
        if problem:
            problems.append(f"{name}:{number}: {problem}: {command}")
    known = _all_options(root)
    for number, line in enumerate(text.splitlines(), start=1):
        for match in _INLINE_SPAN.finditer(line):
            span = match.group(1).strip()
            word = span.split(maxsplit=1)[0].split("=")[0] if span else ""
            if (
                word.startswith("-")
                and word not in {"-", "--"}
                and not _NEGATIVE_NUMBER.match(word)
            ):
                if word not in known:
                    problems.append(f"{name}:{number}: unknown option {word}: {span}")
    return problems


def _synthetic(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def test_skills_and_commands_only_name_what_the_cli_has() -> None:
    files = _markdown_files()
    assert files
    problems: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert _command_lines(text), f"{path.relative_to(ROOT)} names no grepogram command"
        problems += _drift(_CLI, str(path.relative_to(ROOT)), text)
    assert not problems, "\n".join(problems)


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
    problems = _drift(_CLI, "x.md", _synthetic("text", body))
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
    assert _drift(_CLI, "x.md", _synthetic(body)) == []


@pytest.mark.parametrize("fence", _FENCES)
def test_drift_reads_fenced_blocks(fence: str) -> None:
    text = _synthetic(f"{fence}bash", "grepogram search --frob x", fence)
    assert len(_drift(_CLI, "x.md", text)) == 1


@pytest.mark.parametrize(
    "text",
    [
        _synthetic("---", "allowed-tools: Bash(grepogram search:*), Bash(grepogram nope:*)", "---"),
        _synthetic(
            "---",
            "allowed-tools:",
            "  - Bash(grepogram search:*)",
            "  - Bash(grepogram nope:*)",
            "---",
        ),
    ],
    ids=["comma", "list"],
)
def test_drift_checks_allowed_tools_in_list_and_comma_form(text: str) -> None:
    problems = _drift(_CLI, "x.md", text)
    assert len(problems) == 1
    assert "nope" in problems[0]


def test_drift_accepts_good_allowed_tools() -> None:
    good = _synthetic(
        "---", "allowed-tools: Bash(grepogram --version:*), Bash(grepogram sources ls:*)", "---"
    )
    assert _drift(_CLI, "x.md", good) == []


# --- allowed-tools ---------------------------------------------------------------------------

# What runs without a prompt must change nothing: reading the index, the account's chat list and
# research state, plus the local sync and the research steps that only add or narrow candidates.
# Never an approval, a run, an exclusion, a source, an account, the config, the install or the
# MCP registration.
_READ_ONLY = {
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


def _allowed_tools(text: str) -> list[str]:
    """The ``allowed-tools`` entries of a file's frontmatter, in comma or list form."""
    assert text.startswith("---\n"), "the frontmatter must open on the first line"
    front = "\n".join(line for _, line in _frontmatter(text)[0])
    match = re.search(r"^allowed-tools:(.*?)(?=^\S|\Z)", front, re.MULTILINE | re.DOTALL)
    if match is None:
        return []
    entries = (part.strip(" -\t") for part in re.split(r"[,\n]", match.group(1)))
    return [entry for entry in entries if entry]


def _unsafe_tools(text: str) -> list[str]:
    def safe(entry: str) -> bool:
        match = _ALLOWED_TOOL.fullmatch(entry)
        return (
            match is not None
            and match.group(2) is not None
            and match.group(1).removeprefix("grepogram ") in _READ_ONLY
        )

    return [entry for entry in _allowed_tools(text) if not safe(entry)]


def test_allowed_tools_pre_allow_only_commands_that_change_nothing() -> None:
    for path in _markdown_files():
        text = path.read_text(encoding="utf-8")
        assert _allowed_tools(text), path.relative_to(ROOT)
        assert _unsafe_tools(text) == [], path.relative_to(ROOT)


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
@pytest.mark.parametrize("form", ["comma", "list"])
def test_an_allowed_tool_that_can_change_something_is_refused(entry: str, form: str) -> None:
    if form == "comma":
        text = _synthetic("---", f"allowed-tools: Bash(grepogram search:*), {entry}", "---")
    else:
        text = _synthetic(
            "---", "allowed-tools:", "  - Bash(grepogram search:*)", f"  - {entry}", "---"
        )
    assert _unsafe_tools(text) == [entry]


def test_frontmatter_hidden_behind_a_bom_is_refused() -> None:
    """A byte-order mark in front of the opening ``---`` hides the frontmatter from a naive
    reader; the lint must fail loudly rather than see no ``allowed-tools`` and pass."""
    text = "﻿" + _synthetic("---", "allowed-tools: Bash(grepogram leave:*)", "---")
    with pytest.raises(AssertionError):
        _allowed_tools(text)


# --- skill facts -----------------------------------------------------------------------------

# The facts the skills restate from the code.


def test_skill_facts_match_the_code() -> None:
    search_skill = (PLUGIN_DIR / "skills" / "search" / "SKILL.md").read_text(encoding="utf-8")
    research_skill = (PLUGIN_DIR / "skills" / "research" / "SKILL.md").read_text(encoding="utf-8")
    assert f"exits with code {cli.CONFIRM_EXIT}" in research_skill
    assert f"Above {SearchCfg().auto_sync_after_min} minutes" in search_skill
    assert f"{ResearchCfg().run_budget_s // 60} minutes by default" in research_skill

    def names(*types: Any) -> set[str]:
        return {f.name for t in types for f in dataclasses.fields(t)}

    hit_names = names(SearchResult, Hit, MessageView)
    for name in (
        "index_age_min",
        "warnings",
        "url",
        "snippet",
        "chat",
        "anchor_msg_id",
        "date_start",
        "date_end",
        "accounts",
        "chat_id",
        "msg_id",
    ):
        assert name in hit_names and f"`{name}`" in search_skill, name
    candidate_names = names(Candidate, Evidence)
    for name in ("title", "identity", "status", "member", "via", "snippet", "msg_id"):
        assert name in candidate_names and f"`{name}`" in research_skill, name


# --- cli floor -------------------------------------------------------------------------------

# One stated minimum CLI version, the same everywhere, never ahead of us. Loose on purpose, so a
# second floor written another way is counted too.
_FLOOR = re.compile(r"grepogram\s*(?:>=|≥)\s*v?(\d[\w.]*\w)")


def test_cli_floor_is_stated_once_per_file_and_agrees() -> None:
    floors: dict[str, str] = {}
    for path in _markdown_files():
        name = str(path.relative_to(ROOT))
        text = path.read_text(encoding="utf-8")
        found = _FLOOR.findall(text)
        assert len(found) == 1, f"{name} states the floor {len(found)} times"
        assert f"grepogram >= {found[0]}" in text, f"{name} spells the floor another way"
        floors[name] = found[0]
    assert len(set(floors.values())) == 1, floors
    floor = next(iter(floors.values()))
    assert Version(floor) <= Version(grepogram.__version__)


# --- shipped files ---------------------------------------------------------------------------


def _plugin_texts() -> list[tuple[Path, str]]:
    texts = [(path, _text_or_none(path)) for path in PLUGIN_DIR.rglob("*")]
    return [(path, text) for path, text in texts if text is not None]


@pytest.mark.parametrize(
    "pattern",
    [
        r"/(Users|home|opt|usr/local)/",
        r"\.(png|jpe?g|svg|ico|gif|webp)\b",
        rf"`({'|'.join(_INSTALLERS)})\s",
    ],
    ids=["absolute-paths", "image-names", "installers"],
)
def test_plugin_text_matches_no_banned_pattern(pattern: str) -> None:
    for path, text in _plugin_texts():
        assert not re.search(pattern, text, re.IGNORECASE), path


def test_skill_names_match_their_directories() -> None:
    for skill in (PLUGIN_DIR / "skills").glob("*/SKILL.md"):
        text = skill.read_text(encoding="utf-8")
        assert re.search(rf"^name: {skill.parent.name}$", text, re.MULTILINE), skill


def test_setup_is_user_only() -> None:
    setup = (PLUGIN_DIR / "commands" / "setup.md").read_text(encoding="utf-8")
    assert re.search(r"^disable-model-invocation: true$", setup, re.MULTILINE)


def test_privacy_policy_exists_and_the_manifest_names_it() -> None:
    manifest = _load(MANIFEST)
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


def _project_files() -> list[Path]:
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
    name = next(ICON_DIR.glob("*.png")).name
    offenders = [
        str(path.relative_to(ROOT))
        for path in _project_files()
        if name in (_text_or_none(path) or "")
    ]
    assert offenders == []
