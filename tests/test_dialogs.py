import datetime as dt
from collections.abc import Iterator
from pathlib import Path

import pytest
from telethon.tl import functions, types
from typer.testing import CliRunner

from grepogram import cli, dialogs, tg
from grepogram.dialogs import DialogCatalog, DialogInfo, FolderInfo
from grepogram.log import shutdown_logging
from grepogram.paths import Paths
from tests.fakes import (
    FAR_FUTURE,
    FakeClient,
    make_channel,
    make_chatlist,
    make_dialog,
    make_folder,
    make_group,
    make_user,
)

runner = CliRunner()

CONFIG_WITH_KEYS = '[telegram]\napi_id = 12345\napi_hash = "fakehash"\n'
NOW = dt.datetime(2026, 9, 3, 12, 0, tzinfo=dt.UTC)

ALICE = make_user(1, "Alice", "Liddell", username="alice", contact=True)
BOB = make_user(2, "Bob")
HELPER = make_user(3, "Helper", bot=True, username="helper_bot")
OLD_GROUP = make_group(10, "Old group")
ARG = make_channel(100, "Argentina chat", username="arg_chat", megagroup=True, forum=True)
GEORGIA = make_channel(101, "Грузия | Georgia chat", megagroup=True)
NEWS = make_channel(200, "News", username="news")


@pytest.fixture(autouse=True)
def clean_logging() -> Iterator[None]:
    yield
    shutdown_logging()


def _dialogs() -> list:  # type: ignore[type-arg]
    return [
        make_dialog(ALICE),
        make_dialog(BOB, muted=True),
        make_dialog(HELPER),
        make_dialog(OLD_GROUP, archived=True),
        make_dialog(ARG, unread_count=3),
        make_dialog(GEORGIA),
        make_dialog(NEWS),
    ]


def _info(
    dialog_id: int, title: str, kind: str = "supergroup", username: str | None = None
) -> DialogInfo:
    return DialogInfo(id=dialog_id, title=title, type=kind, username=username)  # type: ignore[arg-type]


# --- entities --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entity", "expected"),
    [
        (ALICE, "user"),
        (types.UserEmpty(5), "user"),
        (HELPER, "bot"),
        (OLD_GROUP, "group"),
        (types.ChatForbidden(11, "Kicked"), "group"),
        (types.ChatEmpty(12), "group"),
        (ARG, "supergroup"),
        (NEWS, "channel"),
        (types.ChannelForbidden(300, 300, "Gone", megagroup=True), "supergroup"),
        (types.ChannelForbidden(301, 301, "Gone", broadcast=True), "channel"),
    ],
)
def test_chat_type(entity: object, expected: str) -> None:
    assert dialogs.chat_type(entity) == expected


def test_chat_type_rejects_non_entities() -> None:
    with pytest.raises(TypeError, match="InputPeerEmpty"):
        dialogs.chat_type(types.InputPeerEmpty())


@pytest.mark.parametrize(
    ("entity", "expected"),
    [
        (ALICE, 1),
        (OLD_GROUP, -10),
        (ARG, -1000000000100),
        (types.InputPeerChannel(200, 200), -1000000000200),
        (types.InputPeerChat(10), -10),
        (types.InputPeerUser(1, 1), 1),
    ],
)
def test_peer_id_is_marked(entity: object, expected: int) -> None:
    assert dialogs.peer_id(entity) == expected


def test_entity_username_falls_back_to_active_alias() -> None:
    assert dialogs.entity_username(ALICE) == "alice"
    assert dialogs.entity_username(BOB) is None
    collectible = make_channel(400, "Collectible")
    collectible.usernames = [
        types.Username("inactive", active=False),
        types.Username("active_name", active=True),
    ]
    assert dialogs.entity_username(collectible) == "active_name"
    collectible.username = ""
    assert dialogs.entity_username(collectible) == "active_name"


def test_dialog_info_reads_entity_fields() -> None:
    info = dialogs.dialog_info(ARG, ["Argentina", "LatAm"])
    assert info == DialogInfo(
        id=-1000000000100,
        title="Argentina chat",
        type="supergroup",
        username="arg_chat",
        is_forum=True,
        folders=["Argentina", "LatAm"],
    )
    user = dialogs.dialog_info(ALICE)
    assert (user.title, user.type, user.username, user.is_forum, user.folders) == (
        "Alice Liddell",
        "user",
        "alice",
        False,
        [],
    )


# --- folders ---------------------------------------------------------------------------------


def test_folder_title_handles_text_with_entities_and_plain_strings() -> None:
    assert dialogs.folder_title(make_folder(1, "Argentina")) == "Argentina"
    plain = make_folder(2, "x")
    plain.title = "Plain"
    assert dialogs.folder_title(plain) == "Plain"
    assert dialogs.folder_from_filter(plain).title == "Plain"  # type: ignore[union-attr]


def test_folder_from_filter_reads_peers_and_flags() -> None:
    folder = dialogs.folder_from_filter(
        make_folder(
            3,
            "Argentina",
            include=[ARG],
            pinned=[NEWS, 2],
            exclude=[ALICE],
            contacts=True,
            groups=True,
            exclude_muted=True,
            exclude_archived=True,
        )
    )
    assert folder == FolderInfo(
        id=3,
        title="Argentina",
        include_ids=frozenset({-1000000000100}),
        pinned_ids=frozenset({-1000000000200, 2}),
        exclude_ids=frozenset({1}),
        contacts=True,
        groups=True,
        exclude_muted=True,
        exclude_archived=True,
    )
    assert folder.has_categories


def test_folder_from_filter_chatlist_has_explicit_peers_only() -> None:
    folder = dialogs.folder_from_filter(make_chatlist(4, "Shared", include=[ALICE], pinned=[ARG]))
    assert folder == FolderInfo(
        id=4,
        title="Shared",
        include_ids=frozenset({1}),
        pinned_ids=frozenset({-1000000000100}),
    )
    assert not folder.has_categories


def test_folder_from_filter_skips_default_and_unknown_entries() -> None:
    assert dialogs.folder_from_filter(types.DialogFilterDefault()) is None
    assert dialogs.folder_from_filter(object()) is None


def test_folder_from_filter_resolves_self_and_skips_unknown_peers() -> None:
    tl_folder = make_folder(5, "Me", include=[types.InputPeerSelf(), ALICE])
    tl_folder.exclude_peers = [types.InputPeerEmpty()]
    with_self = dialogs.folder_from_filter(tl_folder, self_id=999)
    assert with_self is not None
    assert with_self.include_ids == frozenset({999, 1})
    assert with_self.exclude_ids == frozenset()
    without = dialogs.folder_from_filter(tl_folder)
    assert without is not None
    assert without.include_ids == frozenset({1})


async def test_fetch_folders_skips_default_and_sends_the_request() -> None:
    client = FakeClient(
        folders=[make_folder(3, "Argentina", include=[ARG]), make_chatlist(4, "Shared")]
    )
    folders = await dialogs.fetch_folders(client)
    assert [f.title for f in folders] == ["Argentina", "Shared"]
    assert folders[0].include_ids == frozenset({-1000000000100})
    assert [type(r) for r in client.requests] == [functions.messages.GetDialogFiltersRequest]
    assert ("get_me", {}) not in client.calls


async def test_fetch_folders_accepts_a_bare_list_result() -> None:
    client = FakeClient(
        responses={functions.messages.GetDialogFiltersRequest: [make_folder(7, "Old layer")]}
    )
    assert [f.title for f in await dialogs.fetch_folders(client)] == ["Old layer"]


async def test_fetch_folders_resolves_input_peer_self_through_get_me() -> None:
    tl_folder = make_folder(5, "Me", include=[types.InputPeerSelf()])
    client = FakeClient(folders=[tl_folder], me=make_user(999, "Me"))
    (folder,) = await dialogs.fetch_folders(client)
    assert folder.include_ids == frozenset({999})
    assert ("get_me", {}) in client.calls
    anonymous = FakeClient(folders=[tl_folder], me=None)
    (folder,) = await dialogs.fetch_folders(anonymous)
    assert folder.include_ids == frozenset()


def test_is_muted_compares_mute_until_with_now() -> None:
    assert dialogs.is_muted(make_dialog(BOB, muted=True), NOW)
    assert not dialogs.is_muted(make_dialog(BOB), NOW)
    expired = make_dialog(BOB, muted=True)
    expired.dialog.notify_settings.mute_until = NOW - dt.timedelta(days=1)
    assert not dialogs.is_muted(expired, NOW)
    naive = make_dialog(BOB, muted=True)
    naive.dialog.notify_settings.mute_until = FAR_FUTURE.replace(tzinfo=None)
    assert dialogs.is_muted(naive, NOW)
    assert dialogs.is_muted(make_dialog(BOB, muted=True))


def test_is_unread_checks_counts_and_mark() -> None:
    assert not dialogs.is_unread(make_dialog(BOB))
    assert dialogs.is_unread(make_dialog(BOB, unread_count=2))
    mentioned = make_dialog(BOB)
    mentioned.unread_mentions_count = 1
    assert dialogs.is_unread(mentioned)
    marked = make_dialog(BOB)
    marked.dialog.unread_mark = True
    assert dialogs.is_unread(marked)


@pytest.mark.parametrize(
    ("entity", "expected"),
    [
        (ALICE, "contacts"),
        (BOB, "non_contacts"),
        (HELPER, "bots"),
        (OLD_GROUP, "groups"),
        (ARG, "groups"),
        (NEWS, "broadcasts"),
    ],
)
def test_category_of(entity: object, expected: str) -> None:
    assert dialogs.category_of(entity) == expected


def test_folder_members_explicit_peers() -> None:
    folder = FolderInfo(
        id=1,
        title="x",
        include_ids=frozenset({-1000000000100, 1}),
        pinned_ids=frozenset({-1000000000200, 424242}),
        exclude_ids=frozenset({1}),
    )
    assert dialogs.folder_members(folder, _dialogs(), now=NOW) == {
        -1000000000100,
        -1000000000200,
        424242,
    }


def test_folder_members_without_categories_ignores_dialogs() -> None:
    assert dialogs.folder_members(FolderInfo(id=1, title="empty"), _dialogs(), now=NOW) == set()


@pytest.mark.parametrize(
    ("flag", "expected"),
    [
        ("contacts", {1}),
        ("non_contacts", {2}),
        ("bots", {3}),
        ("groups", {-10, -1000000000100, -1000000000101}),
        ("broadcasts", {-1000000000200}),
    ],
)
def test_folder_members_category_flags(flag: str, expected: set[int]) -> None:
    folder = FolderInfo(id=1, title="x", **{flag: True})  # type: ignore[arg-type]
    assert dialogs.folder_members(folder, _dialogs(), now=NOW) == expected


def test_folder_members_exclude_peers_beats_category() -> None:
    folder = FolderInfo(id=1, title="x", groups=True, exclude_ids=frozenset({-10}))
    assert dialogs.folder_members(folder, _dialogs(), now=NOW) == {
        -1000000000100,
        -1000000000101,
    }


@pytest.mark.parametrize(
    ("flag", "expected"),
    [
        ("exclude_muted", {1, 3}),
        ("exclude_read", set()),
        ("exclude_archived", {1, 2, 3}),
    ],
)
def test_folder_members_exclude_flags_narrow_category_members(
    flag: str, expected: set[int]
) -> None:
    folder = FolderInfo(
        id=1,
        title="x",
        contacts=True,
        non_contacts=True,
        bots=True,
        **{flag: True},  # type: ignore[arg-type]
    )
    assert dialogs.folder_members(folder, _dialogs(), now=NOW) == expected


def test_folder_members_exclude_flags_apply_to_groups_too() -> None:
    folder = FolderInfo(id=1, title="x", groups=True, exclude_archived=True, exclude_read=True)
    assert dialogs.folder_members(folder, _dialogs(), now=NOW) == {-1000000000100}


def test_folder_members_explicit_peers_survive_exclude_flags() -> None:
    folder = FolderInfo(
        id=1,
        title="x",
        include_ids=frozenset({2}),
        pinned_ids=frozenset({-10}),
        contacts=True,
        exclude_muted=True,
        exclude_archived=True,
        exclude_read=True,
    )
    assert dialogs.folder_members(folder, _dialogs(), now=NOW) == {2, -10}


# --- catalog ---------------------------------------------------------------------------------


def _client(**kwargs: object) -> FakeClient:
    folders = [
        make_folder(3, "Argentina", include=[ARG], pinned=[NEWS]),
        make_folder(4, "People", contacts=True, bots=True, exclude=[HELPER]),
        make_chatlist(5, "Shared", include=[ALICE, ARG]),
    ]
    return FakeClient(dialogs=_dialogs(), folders=folders, **kwargs)  # type: ignore[arg-type]


async def test_catalog_lists_dialogs_with_their_folders() -> None:
    catalog = DialogCatalog(_client())
    infos = await catalog.list_dialogs()
    by_id = {info.id: info for info in infos}
    assert [info.id for info in infos] == [
        1,
        2,
        3,
        -10,
        -1000000000100,
        -1000000000101,
        -1000000000200,
    ]
    assert by_id[1].folders == ["People", "Shared"]
    assert by_id[3].folders == []
    assert by_id[-1000000000100].folders == ["Argentina", "Shared"]
    assert by_id[-1000000000200].folders == ["Argentina"]
    assert by_id[-1000000000100].type == "supergroup"
    assert by_id[-1000000000100].is_forum
    assert [f.title for f in await catalog.list_folders()] == ["Argentina", "People", "Shared"]


async def test_catalog_memoizes_until_invalidated() -> None:
    client = _client()
    catalog = DialogCatalog(client)
    first = await catalog.list_dialogs()
    folders = await catalog.list_folders()
    assert await catalog.list_dialogs() is first
    assert await catalog.list_folders() is folders
    assert client.calls.count(("get_dialogs", {})) == 1
    assert len(client.requests) == 1
    catalog.invalidate()
    again = await catalog.list_dialogs()
    assert again == first
    assert again is not first
    assert client.calls.count(("get_dialogs", {})) == 2
    assert len(client.requests) == 2


async def test_catalog_list_folders_alone_loads_once() -> None:
    client = _client()
    catalog = DialogCatalog(client)
    assert len(await catalog.list_folders()) == 3
    await catalog.list_dialogs()
    assert client.calls.count(("get_dialogs", {})) == 1


async def test_catalog_entity_lookup_falls_back_to_get_entity() -> None:
    client = _client(entities=[make_channel(999, "Elsewhere")])
    catalog = DialogCatalog(client)
    assert (await catalog.entity(-1000000000100)) is ARG
    assert ("get_entity", {"key": -1000000000100}) not in client.calls
    elsewhere = await catalog.entity(-1000000000999)
    assert elsewhere.title == "Elsewhere"
    assert ("get_entity", {"key": -1000000000999}) in client.calls
    with pytest.raises(ValueError):
        await catalog.entity(-1000000000998)


# --- matching --------------------------------------------------------------------------------


INFOS = [
    _info(-1000000000100, "Argentina chat", username="arg_chat"),
    _info(-1000000000101, "Грузия | Georgia chat"),
    _info(-1000000000102, "Argentina"),
    _info(-1000000000103, "Buenos Aires expats"),
    _info(1, "Alice Liddell", "user", username="alice"),
]
FOLDERS = [FolderInfo(id=3, title="Argentina"), FolderInfo(id=4, title="People")]


def test_normalize_folds_case_width_and_whitespace() -> None:
    assert dialogs.normalize("  ＡRGENTINA   Chat ") == "argentina chat"
    assert dialogs.normalize("АРГЕНТИНА") == "аргентина"


def test_match_orders_exact_then_substring_then_fuzzy() -> None:
    found = dialogs.match("argentina", INFOS, FOLDERS)
    assert [(m.kind, m.title) for m in found] == [
        ("dialog", "Argentina"),
        ("folder", "Argentina"),
        ("dialog", "Argentina chat"),
    ]
    assert found[0].score == 1.0
    assert found[1].score == 1.0
    assert 0.8 < found[2].score < 1.0
    assert found[0].dialog is INFOS[2]
    assert found[1].folder is FOLDERS[0]
    assert found[0].folder is None
    assert found[1].dialog is None


def test_match_fuzzy_catches_typos_but_not_unrelated_titles() -> None:
    typo = dialogs.match("georgai", INFOS, FOLDERS)
    assert [m.title for m in typo] == ["Грузия | Georgia chat"]
    assert 0 < typo[0].score < 0.8
    assert dialogs.match("bank account", INFOS, FOLDERS) == []


def test_match_ranks_substring_above_fuzzy() -> None:
    found = dialogs.match("argentna", INFOS, FOLDERS)
    assert [m.title for m in found][:2] == ["Argentina", "Argentina"]
    assert all(m.score < 0.8 for m in found)
    with_sub = dialogs.match("arg", INFOS, FOLDERS)
    assert with_sub[0].title == "Argentina chat"
    assert all(m.score > 0.8 for m in with_sub)


def test_match_by_username_with_or_without_at() -> None:
    assert [m.title for m in dialogs.match("@alice", INFOS, FOLDERS)] == ["Alice Liddell"]
    upper = dialogs.match("ALICE", INFOS, FOLDERS)
    assert upper[0].title == "Alice Liddell"
    assert upper[0].score == 1.0
    assert [m.title for m in upper[1:]] == ["Buenos Aires expats"]
    assert dialogs.match("@alice", INFOS, FOLDERS)[0].score == 1.0
    by_handle = dialogs.match("arg_chat", INFOS, FOLDERS)
    assert by_handle[0].title == "Argentina chat"
    assert by_handle[0].score == 1.0
    assert all(m.score < 0.8 for m in by_handle[1:])


def test_match_is_case_insensitive_across_scripts() -> None:
    assert [m.title for m in dialogs.match("ГРУЗИЯ", INFOS)] == ["Грузия | Georgia chat"]
    assert [m.title for m in dialogs.match("ＡRGENTINA", INFOS)][0] == "Argentina"


def test_match_limit_and_empty_query() -> None:
    assert len(dialogs.match("a", INFOS, FOLDERS, limit=2)) == 2
    assert len(dialogs.match("a", INFOS, FOLDERS)) == 6
    assert dialogs.match("   ", INFOS, FOLDERS) == []
    assert dialogs.match("@", INFOS, FOLDERS) == []
    assert dialogs.match("argentina", [], []) == []


# --- CLI -------------------------------------------------------------------------------------


def _signed_in(tmp_home: Path) -> Path:
    (tmp_home / "config.toml").write_text(CONFIG_WITH_KEYS, encoding="utf-8")
    session = Paths.from_env().session_file
    session.touch()
    return session


def test_cli_dialogs_refuses_without_api_keys(tmp_home: Path) -> None:
    result = runner.invoke(cli.app, ["dialogs", "argentina"])
    assert result.exit_code == 1
    assert "my.telegram.org" in result.stderr
    assert result.stdout == ""


def test_cli_dialogs_requires_a_session(tmp_home: Path) -> None:
    (tmp_home / "config.toml").write_text(CONFIG_WITH_KEYS, encoding="utf-8")
    result = runner.invoke(cli.app, ["dialogs", "argentina"])
    assert result.exit_code == 1
    assert "no Telegram session" in result.stderr
    assert "run: grepogram auth" in result.stderr


def test_cli_dialogs_prints_a_table(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _signed_in(tmp_home)
    fake = _client()
    monkeypatch.setattr(tg, "make_client", lambda cfg, paths: fake)
    result = runner.invoke(cli.app, ["dialogs", "arg"])
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0].split() == ["kind", "id", "type", "title", "username", "folders", "score"]
    assert lines[1].startswith("dialog  -1000000000100  supergroup  Argentina chat  @arg_chat")
    assert "Argentina, Shared" in lines[1]
    assert lines[2].startswith("folder  3")
    assert "Argentina" in lines[2]
    assert not any(line.endswith(" ") for line in lines)
    assert ("connect", {}) in fake.calls
    assert fake.calls[-1] == ("disconnect", {})


def test_cli_dialogs_honours_limit_and_reports_no_matches(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _signed_in(tmp_home)
    monkeypatch.setattr(tg, "make_client", lambda cfg, paths: _client())
    limited = runner.invoke(cli.app, ["dialogs", "a", "-n", "1"])
    assert limited.exit_code == 0, limited.output
    assert len(limited.stdout.splitlines()) == 2
    nothing = runner.invoke(cli.app, ["dialogs", "zzzz"])
    assert nothing.exit_code == 0, nothing.output
    assert nothing.stdout.strip() == "no dialogs or folders match 'zzzz'"


def test_cli_dialogs_maps_auth_and_network_errors(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _signed_in(tmp_home)
    monkeypatch.setattr(tg, "make_client", lambda cfg, paths: _client(authorized=False))
    unauthorized = runner.invoke(cli.app, ["dialogs", "arg"])
    assert unauthorized.exit_code == 1
    assert "run: grepogram auth" in unauthorized.stderr
    broken = _client()

    async def failing_connect() -> None:
        raise ConnectionError("no route to Telegram")

    monkeypatch.setattr(broken, "connect", failing_connect)
    monkeypatch.setattr(tg, "make_client", lambda cfg, paths: broken)
    offline = runner.invoke(cli.app, ["dialogs", "arg"])
    assert offline.exit_code == 1
    assert "telegram error: no route to Telegram" in offline.stderr
