import datetime as dt
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from telethon import errors
from telethon.tl import functions, types
from typer.testing import CliRunner

from grepogram import cli, tg
from grepogram.log import shutdown_logging
from grepogram.models import Config, SyncCfg, TelegramCfg
from grepogram.paths import Paths
from tests.fakes import (
    FakeClient,
    make_channel,
    make_dialog,
    make_group,
    make_message,
    make_user,
)

runner = CliRunner()

CONFIG_WITH_KEYS = '[telegram]\napi_id = 12345\napi_hash = "fakehash"\n'


@pytest.fixture(autouse=True)
def clean_logging() -> Iterator[None]:
    yield
    shutdown_logging()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _paths(tmp_path: Path) -> Paths:
    return Paths.under(tmp_path / "home")


# --- errors ----------------------------------------------------------------------------------


def test_auth_required_carries_the_hint() -> None:
    exc = tg.AuthRequired()
    assert "run: grepogram auth" in str(exc)
    assert exc.hint == "run: grepogram auth"
    custom = tg.AuthRequired("session revoked")
    assert str(custom) == "session revoked (run: grepogram auth)"
    assert custom.reason == "session revoked"


def test_session_missing_is_an_auth_error_not_a_file_error(tmp_path: Path) -> None:
    exc = tg.SessionMissing(tmp_path / "s.session")
    assert isinstance(exc, tg.AuthRequired)
    assert not isinstance(exc, FileNotFoundError)
    assert exc.path == tmp_path / "s.session"
    assert str(tmp_path / "s.session") in str(exc)
    assert "run: grepogram auth" in str(exc)


# --- make_client -----------------------------------------------------------------------------


def test_make_client_uses_session_path_and_config(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.ensure_dirs()
    cfg = Config(
        telegram=TelegramCfg(api_id=777, api_hash="hash"), sync=SyncCfg(flood_sleep_threshold=45)
    )
    client = tg.make_client(cfg, paths)
    try:
        assert client.session.filename == str(paths.session_file)
        assert client.api_id == 777
        assert client.api_hash == "hash"
        assert client.flood_sleep_threshold == 45
        assert client._init_request.device_model == "grepogram"
        assert not client.is_connected()
    finally:
        client.session.close()


def test_prepare_session_keeps_the_file_private_when_telethon_opens_it(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    assert tg.prepare_session(paths) == paths.session_file
    assert _mode(paths.session_file) == 0o600
    client = tg.make_client(Config(telegram=TelegramCfg(api_id=1, api_hash="h")), paths)
    try:
        assert paths.session_file.stat().st_size > 0
        assert _mode(paths.session_file) == 0o600
    finally:
        client.session.close()


def test_prepare_session_fixes_the_mode_of_an_existing_file(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.ensure_dirs()
    paths.session_file.write_bytes(b"keep me")
    paths.session_file.chmod(0o644)
    tg.prepare_session(paths)
    assert paths.session_file.read_bytes() == b"keep me"
    assert _mode(paths.session_file) == 0o600


# --- ensure_session_mode ---------------------------------------------------------------------


def test_ensure_session_mode_sets_0600_on_an_existing_file(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.ensure_dirs()
    paths.session_file.write_bytes(b"")
    paths.session_file.chmod(0o644)
    assert tg.ensure_session_mode(paths) == paths.session_file
    assert _mode(paths.session_file) == 0o600
    tg.ensure_session_mode(paths)
    assert _mode(paths.session_file) == 0o600


def test_ensure_session_mode_raises_session_missing(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    with pytest.raises(tg.SessionMissing) as excinfo:
        tg.ensure_session_mode(paths)
    assert not isinstance(excinfo.value, FileNotFoundError)
    assert excinfo.value.path == paths.session_file
    assert excinfo.value.__cause__ is None


# --- wrap_auth_errors ------------------------------------------------------------------------


@pytest.mark.parametrize("error_cls", tg.AUTH_ERRORS, ids=lambda cls: cls.__name__)
async def test_wrap_auth_errors_maps_dead_session_errors(error_cls: type[Any]) -> None:
    client = FakeClient()
    with pytest.raises(tg.AuthRequired) as excinfo:
        async with tg.wrap_auth_errors(client):
            raise error_cls(request=None)
    assert isinstance(excinfo.value.__cause__, error_cls)
    assert "rejected" in str(excinfo.value)
    assert "run: grepogram auth" in str(excinfo.value)


@pytest.mark.parametrize(
    "error",
    [ValueError("unrelated"), errors.FloodWaitError(request=None, capture=30)],
    ids=["ValueError", "FloodWaitError"],
)
async def test_wrap_auth_errors_passes_other_errors_through(error: Exception) -> None:
    client = FakeClient()
    with pytest.raises(type(error)) as excinfo:
        async with tg.wrap_auth_errors(client):
            raise error
    assert excinfo.value is error


async def test_wrap_auth_errors_rejects_an_unauthorized_client_before_the_body() -> None:
    client = FakeClient(authorized=False)
    ran = False
    with pytest.raises(tg.AuthRequired) as excinfo:
        async with tg.wrap_auth_errors(client):
            ran = True
    assert not ran
    assert excinfo.value.__cause__ is None
    assert ("is_user_authorized", {}) in client.calls


async def test_wrap_auth_errors_runs_the_body_for_an_authorized_client() -> None:
    client = FakeClient()
    ran = False
    async with tg.wrap_auth_errors(client):
        ran = True
    assert ran


# --- connected -------------------------------------------------------------------------------


async def test_connected_connects_yields_and_disconnects() -> None:
    client = FakeClient()
    async with tg.connected(client) as inner:
        assert inner is client
        assert client.is_connected()
    assert not client.is_connected()
    names = [name for name, _ in client.calls]
    assert names == ["connect", "is_user_authorized", "disconnect"]


async def test_connected_disconnects_when_the_body_raises() -> None:
    client = FakeClient()
    with pytest.raises(tg.AuthRequired):
        async with tg.connected(client):
            raise errors.SessionRevokedError(request=None)
    assert not client.is_connected()


async def test_connected_disconnects_an_unauthorized_client() -> None:
    client = FakeClient(authorized=False)
    with pytest.raises(tg.AuthRequired):
        async with tg.connected(client):
            pass
    assert not client.is_connected()


# --- login -----------------------------------------------------------------------------------


async def test_login_runs_all_prompts_for_a_two_factor_account() -> None:
    client = FakeClient(
        authorized=False, two_factor=True, me=make_user(1, "Ann", "Lee", username="ann")
    )
    name = await tg.login(
        client, phone=lambda: "+15551234567", code=lambda: "12345", password=lambda: "hunter2"
    )
    assert name == "Ann Lee"
    assert client.start_inputs == {"phone": "+15551234567", "code": "12345", "password": "hunter2"}
    assert client.authorized
    assert not client.is_connected()


async def test_login_skips_prompts_for_an_authorized_session() -> None:
    client = FakeClient(me=make_user(1, "Ann"))
    asked: list[str] = []

    def ask(label: str) -> str:
        asked.append(label)
        return label

    name = await tg.login(
        client, phone=lambda: ask("phone"), code=lambda: ask("code"), password=lambda: ask("pw")
    )
    assert name == "Ann"
    assert asked == []


async def test_login_without_a_user_raises_auth_required() -> None:
    client = FakeClient(authorized=False, two_factor=False, me=None)
    with pytest.raises(tg.AuthRequired):
        await tg.login(client, phone=lambda: "+1", code=lambda: "1", password=lambda: "")
    assert not client.is_connected()


# --- auth CLI --------------------------------------------------------------------------------


def test_auth_refuses_without_api_id(tmp_home: Path) -> None:
    result = runner.invoke(cli.app, ["auth"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "my.telegram.org" in result.stderr
    assert "config init" in result.stderr
    assert str(tmp_home / "config.toml") in result.stderr
    assert not (tmp_home / "session.session").exists()


def test_auth_refuses_with_api_id_but_no_hash(tmp_home: Path) -> None:
    (tmp_home / "config.toml").write_text("[telegram]\napi_id = 12345\n", encoding="utf-8")
    result = runner.invoke(cli.app, ["auth"])
    assert result.exit_code == 1
    assert "api_hash" in result.stderr
    assert not (tmp_home / "session.session").exists()


def test_auth_fails_on_a_broken_config(tmp_home: Path) -> None:
    (tmp_home / "config.toml").write_text("[telegram]\nbogus = 1\n", encoding="utf-8")
    result = runner.invoke(cli.app, ["auth"])
    assert result.exit_code == 1
    assert "telegram.bogus" in result.stderr


def test_auth_signs_in_with_prompts_and_stores_a_private_session(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_home / "config.toml").write_text(CONFIG_WITH_KEYS, encoding="utf-8")
    fake = FakeClient(authorized=False, two_factor=True, me=make_user(1, "Ann", "Lee"))
    seen: dict[str, object] = {}

    def make_client(cfg: Config, paths: Paths) -> FakeClient:
        seen["cfg"] = cfg
        seen["paths"] = paths
        return fake

    monkeypatch.setattr(tg, "make_client", make_client)
    result = runner.invoke(cli.app, ["auth"], input="+15551234567\n12345\nhunter2\n")
    assert result.exit_code == 0, result.output
    assert "signed in as Ann Lee" in result.stdout
    assert str(tmp_home / "session.session") in result.stdout
    assert fake.start_inputs == {"phone": "+15551234567", "code": "12345", "password": "hunter2"}
    assert seen["cfg"].telegram == TelegramCfg(api_id=12345, api_hash="fakehash")  # type: ignore[attr-defined]
    assert seen["paths"] == Paths.from_env()
    assert _mode(tmp_home / "session.session") == 0o600
    assert not fake.is_connected()


def test_auth_without_prompts_when_already_signed_in(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_home / "config.toml").write_text(CONFIG_WITH_KEYS, encoding="utf-8")
    fake = FakeClient(me=make_user(1, "Ann"))
    monkeypatch.setattr(tg, "make_client", lambda cfg, paths: fake)
    result = runner.invoke(cli.app, ["auth"], input="")
    assert result.exit_code == 0, result.output
    assert "signed in as Ann" in result.stdout
    assert fake.start_inputs == {}


def test_auth_reports_a_failed_sign_in(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_home / "config.toml").write_text(CONFIG_WITH_KEYS, encoding="utf-8")
    fake = FakeClient(authorized=False)

    async def failing_start(*_: object, **__: object) -> FakeClient:
        raise errors.PhoneNumberInvalidError(request=None)

    monkeypatch.setattr(fake, "start", failing_start)
    monkeypatch.setattr(tg, "make_client", lambda cfg, paths: fake)
    result = runner.invoke(cli.app, ["auth"], input="+1\n")
    assert result.exit_code == 1
    assert "sign-in failed" in result.stderr
    assert "signed in" not in result.stdout


# --- FakeClient ------------------------------------------------------------------------------

GROUP = -1000000000200
CHANNEL = -1000000000300
DISCUSSION = -1000000000400


def _client() -> FakeClient:
    supergroup = make_channel(200, "Argentina", username="ru_argentina", megagroup=True)
    channel = make_channel(300, "News", username="news_channel")
    return FakeClient(
        dialogs=[make_dialog(supergroup, pinned=True), make_dialog(make_user(7, "Bob"))],
        entities=[channel, make_group(5, "Old group")],
        messages={GROUP: [make_message(GROUP, i, f"m{i}") for i in (3, 1, 2, 4, 5)]},
        comments={(CHANNEL, 10): [make_message(DISCUSSION, 100, "comment")]},
    )


async def _collect(client: FakeClient, entity: object, **kwargs: Any) -> list[int]:
    return [m.id async for m in client.iter_messages(entity, **kwargs) if m is not None]


async def test_fake_iter_messages_defaults_to_newest_first() -> None:
    assert await _collect(_client(), GROUP) == [5, 4, 3, 2, 1]


async def test_fake_iter_messages_reverse_with_min_id_and_limit() -> None:
    client = _client()
    assert await _collect(client, GROUP, min_id=2, reverse=True) == [3, 4, 5]
    assert await _collect(client, GROUP, reverse=True, limit=2) == [1, 2]
    assert await _collect(client, GROUP, max_id=3) == [2, 1]
    assert await _collect(client, GROUP, reverse=True, offset_id=3) == [4, 5]
    assert client.calls[-1][0] == "iter_messages"
    assert client.calls[-1][1]["chat_id"] == GROUP


async def test_fake_iter_messages_offset_date_is_exclusive_and_flips_with_reverse() -> None:
    client = _client()
    cutoff = dt.datetime(2025, 1, 1, tzinfo=dt.UTC) + dt.timedelta(minutes=3)
    assert await _collect(client, GROUP, offset_date=cutoff, reverse=True) == [4, 5]
    assert await _collect(client, GROUP, offset_date=cutoff) == [2, 1]


async def test_fake_iter_messages_accepts_entities_and_usernames() -> None:
    client = _client()
    entity = await client.get_entity("@ru_argentina")
    assert await _collect(client, entity, limit=1) == [5]
    assert await _collect(client, "https://t.me/ru_argentina", limit=1) == [5]
    assert await _collect(client, types.PeerChannel(200), limit=1) == [5]
    assert await _collect(client, CHANNEL) == []


async def test_fake_iter_messages_by_ids_yields_none_for_missing() -> None:
    client = _client()
    got = [m async for m in client.iter_messages(GROUP, ids=[2, 99])]
    assert [m.id if m is not None else None for m in got] == [2, None]


async def test_fake_iter_messages_reply_to_uses_comments_or_raises() -> None:
    client = _client()
    assert await _collect(client, CHANNEL, reply_to=10) == [100]
    with pytest.raises(errors.MsgIdInvalidError):
        await _collect(client, CHANNEL, reply_to=11)


async def test_fake_iter_messages_raises_configured_failures() -> None:
    client = _client()
    client.failures[GROUP] = errors.ChannelPrivateError(request=None)
    with pytest.raises(errors.ChannelPrivateError):
        await _collect(client, GROUP)


async def test_fake_get_entity_resolves_ids_usernames_and_links() -> None:
    client = _client()
    assert (await client.get_entity(GROUP)).title == "Argentina"
    assert (await client.get_entity(str(GROUP))).title == "Argentina"
    assert (await client.get_entity("@News_Channel")).title == "News"
    assert (await client.get_entity("https://t.me/ru_argentina")).title == "Argentina"
    assert (await client.get_entity(-5)).title == "Old group"
    assert (await client.get_entity(types.PeerUser(7))).first_name == "Bob"
    with pytest.raises(ValueError, match="Could not find"):
        await client.get_entity("@nobody")
    with pytest.raises(ValueError, match="Could not find"):
        await client.get_entity(123)


async def test_fake_get_dialogs_returns_client_less_dialogs() -> None:
    client = _client()
    dialogs = await client.get_dialogs()
    assert [d.name for d in dialogs] == ["Argentina", "Bob"]
    assert dialogs[0].id == GROUP
    assert dialogs[0].pinned and dialogs[0].is_group and dialogs[0].is_channel
    assert dialogs[1].is_user and not dialogs[1].pinned
    muted = make_dialog(make_user(8, "Mute"), muted=True, archived=True, unread_count=3)
    assert muted.archived and muted.unread_count == 3
    assert muted.dialog.notify_settings.mute_until is not None


async def test_fake_call_dispatches_raw_requests() -> None:
    full = types.messages.ChatFull(
        full_chat=types.ChannelFull(
            id=300,
            about="",
            read_inbox_max_id=0,
            read_outbox_max_id=0,
            unread_count=0,
            chat_photo=types.PhotoEmpty(id=0),
            notify_settings=types.PeerNotifySettings(),
            bot_info=[],
            pts=0,
            participants_count=0,
            linked_chat_id=400,
        ),
        chats=[],
        users=[],
    )
    client = FakeClient(
        responses={
            functions.channels.GetFullChannelRequest: full,
            functions.messages.GetDialogFiltersRequest: lambda request: [request],
            functions.updates.GetStateRequest: errors.AuthKeyUnregisteredError(request=None),
        }
    )
    request = functions.channels.GetFullChannelRequest(types.InputChannel(300, 300))
    assert (await client(request)).full_chat.linked_chat_id == 400
    filters_request = functions.messages.GetDialogFiltersRequest()
    assert await client(filters_request) == [filters_request]
    with pytest.raises(errors.AuthKeyUnregisteredError):
        await client(functions.updates.GetStateRequest())
    with pytest.raises(NotImplementedError, match="GetConfigRequest"):
        await client(functions.help.GetConfigRequest())
    assert client.requests[0] is request
