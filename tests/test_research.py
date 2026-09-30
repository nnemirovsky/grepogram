import asyncio
import copy
import dataclasses
import datetime as dt
import functools
import logging
import re
import sqlite3
import threading
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from telethon import errors, utils
from telethon.tl import functions, types

from grepogram import config, db, leads, research, research_db, sync, tg
from grepogram.filters import UnknownChat
from grepogram.models import (
    AccountCfg,
    ApprovalItem,
    Candidate,
    CandidateView,
    ChatKey,
    ChatRow,
    Config,
    Grant,
    LinkKind,
    MessageRow,
    ResearchCfg,
    ResearchLimits,
    ResearchSession,
    RunReport,
    Source,
)
from grepogram.paths import Paths
from tests.conftest import scan_cursor
from tests.fakes import (
    FakeChatlist,
    FakeClient,
    FakeInvite,
    FakeWorld,
    make_channel,
    make_group,
    make_user,
    no_discussion,
)
from tests.fixtures import tl, two_accounts

SEED = -1000000000100
SEED_TWO = -1000000000101
ORIGIN = -1000000000300
CACHED = -1000000000400
HOP = -1000000000500
HOP_ORIGIN = -1000000000600
DEEP_ORIGIN = -1000000000700

CFG = Config(research=ResearchCfg(enabled=True))
QUESTION = "who rents apartments in Tbilisi"


@pytest.fixture
def rdb() -> Iterator[sqlite3.Connection]:
    connection = research_db.open_store(":memory:")
    yield connection
    connection.close()


@pytest.fixture
def conn(conn: sqlite3.Connection) -> sqlite3.Connection:
    for chat in (
        ChatRow(id=SEED, type="channel", title="Tbilisi rent", username="TbRent"),
        ChatRow(id=SEED_TWO, type="supergroup", title="Tbilisi chat", username="tbchat"),
        ChatRow(id=CACHED, type="channel", title="Already here", username="CachedChan"),
    ):
        db.upsert_chat(conn, chat)
    db.set_chat_access(conn, SEED, "default")
    db.set_chat_access(conn, CACHED, "work")
    return conn


def _store(
    conn: sqlite3.Connection,
    chat_id: int,
    msg_id: int,
    text: str = "",
    links: tuple[tuple[LinkKind, str], ...] | None = (),
    **fields: Any,
) -> None:
    row = MessageRow(chat_id=chat_id, msg_id=msg_id, date=1_700_000_000 + msg_id, text=text)
    db.upsert_messages(conn, [dataclasses.replace(row, links=links, **fields)])


def _start(
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    seeds: tuple[str, ...] = (str(SEED),),
    **limits: int,
) -> ResearchSession:
    return research.start_session(rdb, conn, CFG, QUESTION, list(seeds), "default", limits, now=1)


def _asking(rdb: sqlite3.Connection, conn: sqlite3.Connection, question: str) -> ResearchSession:
    """A session whose question is ``question``: the one query its global search may send."""
    return research.start_session(rdb, conn, CFG, question, [str(SEED)], "default", now=1)


def _by_identity(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, session: ResearchSession
) -> dict[str, CandidateView]:
    return {v.candidate.identity: v for v in research.candidate_views(rdb, conn, session)}


def _register(
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    session: ResearchSession,
    chat_id: int,
    depth: int,
) -> None:
    """Register an indexed chat as one the session reads, at ``depth`` — what a run does for a
    chat it fetched."""
    chat = db.get_chat(conn, chat_id)
    assert chat is not None
    research_db.set_scan_cursor(
        rdb, session.id, research.chat_key(chat), depth=depth, index_id=db.index_id(conn)
    )


# --- sessions --------------------------------------------------------------------------------


def test_start_refuses_while_research_is_disabled(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    with pytest.raises(research.ResearchDisabled) as caught:
        research.start_session(rdb, conn, Config(), QUESTION, [str(SEED)], "default")
    assert "enabled = true" in (caught.value.hint or "")
    assert research_db.list_sessions(rdb) == []


def test_discover_refuses_while_research_is_disabled(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    session = _start(rdb, conn)
    with pytest.raises(research.ResearchDisabled):
        research.discover_offline(rdb, conn, Config(), session.id)


def test_start_resolves_seeds_through_chat_specs(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    session = _start(rdb, conn, seeds=("@tbrent", "Tbilisi chat"))
    assert session.seeds == (ChatKey("", SEED_TWO), ChatKey("", SEED))
    assert session.account == "default"
    assert research_db.get_session(rdb, session.id) == session


def test_start_takes_the_configured_limits_by_default(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    cfg = Config(research=ResearchCfg(enabled=True, max_depth=5, max_candidates=7))
    session = research.start_session(rdb, conn, cfg, QUESTION, [str(SEED)], "default")
    assert session.limits == cfg.research.limits()


@pytest.mark.parametrize(
    ("question", "seeds", "account", "message"),
    [
        ("   ", [str(SEED)], "default", "needs a question"),
        (QUESTION, [], "default", "at least one seed"),
        (QUESTION, [str(SEED)], "work", "unknown account 'work'"),
    ],
)
def test_start_refuses_a_malformed_session(
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    question: str,
    seeds: list[str],
    account: str,
    message: str,
) -> None:
    with pytest.raises(research.ResearchError, match=message):
        research.start_session(rdb, conn, CFG, question, seeds, account)
    assert research_db.list_sessions(rdb) == []


def test_start_refuses_a_seed_the_index_does_not_hold(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    with pytest.raises(UnknownChat):
        _start(rdb, conn, seeds=("@nowhere_at_all",))


def test_a_stopped_or_unknown_session_does_not_discover(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    session = _start(rdb, conn)
    research_db.stop_session(rdb, session.id)
    with pytest.raises(research.SessionStopped):
        research.discover_offline(rdb, conn, CFG, session.id)
    with pytest.raises(research.UnknownSession):
        research.discover_offline(rdb, conn, CFG, 999)


# --- leads -----------------------------------------------------------------------------------


def test_every_kind_of_link_becomes_a_candidate_with_its_path(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    _store(conn, SEED, 1, "rentals at t.me/alpha_rent", links=(("link", "@alpha_rent"),))
    _store(conn, SEED, 2, "look here", links=(("text_url", "@hidden_rent"),))
    _store(conn, SEED, 3, "join us", links=(("button", "+AbCdEf_12"),))
    _store(conn, SEED, 4, "a folder", links=(("webpage", "addlist/Tbilisi1"),))
    _store(conn, SEED, 5, "private post", links=(("link", "c/2000/5"),))
    _store(conn, SEED, 6, "ask @Mentioned_Chat", links=(("mention", "@mentioned_chat"),))
    session = _start(rdb, conn)

    report = research.discover_offline(rdb, conn, CFG, session.id, now=5)

    found = _by_identity(rdb, conn, session)
    assert {identity: (v.candidate.kind, v.evidence[0].via) for identity, v in found.items()} == {
        "@alpha_rent": ("username", "link"),
        "@hidden_rent": ("username", "text_url"),
        "+AbCdEf_12": ("invite", "button"),
        "addlist/Tbilisi1": ("addlist", "webpage"),
        "peer:-1000000002000": ("peer", "link"),
        "@mentioned_chat": ("username", "mention"),
    }
    assert all(v.candidate.depth == 1 and v.candidate.status == "proposed" for v in found.values())
    assert found["+AbCdEf_12"].candidate.invite_hash == "AbCdEf_12"
    assert found["addlist/Tbilisi1"].candidate.addlist_slug == "Tbilisi1"
    assert found["peer:-1000000002000"].candidate.peer_id == -1000000002000
    assert found["@alpha_rent"].candidate.username == "alpha_rent"
    evidence = found["@hidden_rent"].evidence[0]
    assert (evidence.chat, evidence.msg_id, evidence.snippet) == (ChatKey("", SEED), 2, "look here")
    assert evidence.origin_key == f"post:{SEED}/2"
    assert report.chats_scanned == 1 and report.messages_scanned == 6 and report.leads == 6
    assert sorted(report.new_candidates) == sorted(v.candidate.id for v in found.values())
    assert report.text_fallback == 0 and not report.truncated


def test_a_post_link_leads_to_its_chat(rdb: sqlite3.Connection, conn: sqlite3.Connection) -> None:
    _store(conn, SEED, 1, "see t.me/alpha_rent/12", links=(("link", "@alpha_rent/12"),))
    _store(conn, SEED, 2, "and t.me/alpha_rent", links=(("link", "@alpha_rent"),))
    session = _start(rdb, conn)
    research.discover_offline(rdb, conn, CFG, session.id)
    (view,) = research.candidate_views(rdb, conn, session)
    assert view.candidate.identity == "@alpha_rent"
    assert view.corroboration == 2
    assert [e.msg_id for e in view.evidence] == [1, 2]


def test_a_forward_leads_to_its_origin(rdb: sqlite3.Connection, conn: sqlite3.Connection) -> None:
    _store(conn, SEED, 1, "flat for rent", fwd_from="Origin", fwd_peer_id=ORIGIN, fwd_msg_id=7)
    session = _start(rdb, conn)
    research.discover_offline(rdb, conn, CFG, session.id)
    (view,) = research.candidate_views(rdb, conn, session)
    assert view.candidate.identity == f"peer:{ORIGIN}"
    assert view.candidate.kind == "peer" and view.candidate.peer_id == ORIGIN
    (evidence,) = view.evidence
    assert (evidence.via, evidence.origin_key) == ("forward", f"post:{ORIGIN}/7")


def test_ten_forwards_of_one_post_are_one_corroboration(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    for msg_id in range(1, 6):
        _store(conn, SEED, msg_id, "same post", fwd_peer_id=ORIGIN, fwd_msg_id=7)
    for msg_id in range(1, 6):
        _store(conn, SEED_TWO, msg_id, "same post", fwd_peer_id=ORIGIN, fwd_msg_id=7)
    _store(conn, SEED, 10, "another post", fwd_peer_id=ORIGIN, fwd_msg_id=8)
    session = _start(rdb, conn, seeds=(str(SEED), str(SEED_TWO)))

    research.discover_offline(rdb, conn, CFG, session.id)

    (view,) = research.candidate_views(rdb, conn, session)
    assert len(view.evidence) == 11
    assert view.corroboration == 2, "ten forwards of post 7 count once, post 8 once more"


def test_a_link_inside_forwarded_copies_counts_once_with_the_original(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    db.upsert_chat(conn, ChatRow(id=ORIGIN, type="channel", title="Origin"))
    _store(conn, ORIGIN, 7, "t.me/alpha_rent", links=(("link", "@alpha_rent"),))
    for msg_id in (1, 2, 3):
        _store(
            conn,
            SEED,
            msg_id,
            "t.me/alpha_rent",
            links=(("link", "@alpha_rent"),),
            fwd_peer_id=ORIGIN,
            fwd_msg_id=7,
        )
    session = _start(rdb, conn, seeds=(str(SEED), str(ORIGIN)))
    research.discover_offline(rdb, conn, CFG, session.id)
    found = _by_identity(rdb, conn, session)
    assert set(found) == {"@alpha_rent"}, "the origin is a seed, so no candidate of its own"
    assert found["@alpha_rent"].corroboration == 1
    assert len(found["@alpha_rent"].evidence) == 4


def test_an_author_only_forward_is_keyed_by_its_original_date(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    for msg_id in (1, 2):
        _store(conn, SEED, msg_id, "hi", fwd_peer_id=ORIGIN, fwd_date=1_600_000_000)
    _store(conn, SEED, 3, "hi again", fwd_peer_id=ORIGIN, fwd_date=1_600_000_500)
    session = _start(rdb, conn)
    research.discover_offline(rdb, conn, CFG, session.id)
    (view,) = research.candidate_views(rdb, conn, session)
    assert view.corroboration == 2
    assert {e.origin_key for e in view.evidence} == {
        f"fwd:{ORIGIN}@1600000000",
        f"fwd:{ORIGIN}@1600000500",
    }


def test_people_and_the_session_s_own_chats_are_not_candidates(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    _store(conn, SEED, 1, "ask Bob", links=(("mention", "peer:4242"),))
    _store(conn, SEED, 2, "from a person", fwd_peer_id=4242, fwd_date=1)
    _store(conn, SEED, 3, "our own t.me/tbrent/1", links=(("link", "@tbrent/1"),))
    _store(conn, SEED, 4, "our chat t.me/TbChat", links=(("link", "@tbchat"),))
    session = _start(rdb, conn, seeds=(str(SEED), str(SEED_TWO)))

    report = research.discover_offline(rdb, conn, CFG, session.id)

    assert research.candidate_views(rdb, conn, session) == []
    assert report.people == 2
    assert report.in_session == 1, "a link to the other seed"
    assert report.leads == 0 and report.new_candidates == []


def test_an_excluded_target_is_never_proposed(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    research_db.add_exclusion(rdb, "@spam_chan", "spam")
    _store(conn, SEED, 1, "t.me/spam_chan", links=(("link", "@spam_chan"),))
    _store(conn, SEED, 2, "t.me/alpha_rent", links=(("link", "@alpha_rent"),))
    session = _start(rdb, conn)

    report = research.discover_offline(rdb, conn, CFG, session.id)

    assert set(_by_identity(rdb, conn, session)) == {"@alpha_rent"}
    assert report.excluded == 1


def test_an_indexed_chat_is_marked_cached_with_its_accounts(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    _store(conn, SEED, 1, "t.me/cachedchan", links=(("link", "@cachedchan"),))
    _store(conn, SEED, 2, "forwarded", fwd_peer_id=CACHED, fwd_msg_id=3)
    _store(conn, SEED, 3, "t.me/alpha_rent", links=(("link", "@alpha_rent"),))
    session = _start(rdb, conn)

    research.discover_offline(rdb, conn, CFG, session.id)

    found = _by_identity(rdb, conn, session)
    for identity in ("@cachedchan", f"peer:{CACHED}"):
        view = found[identity]
        assert view.cached and view.cached_chats == (CACHED,)
        assert view.cached_accounts == ("work",)
        assert view.candidate.member is None, "cached says nothing about membership"
    assert not found["@alpha_rent"].cached
    assert found["@alpha_rent"].cached_accounts == ()


def test_an_imported_chat_is_cached_through_no_account(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    db.upsert_chat(conn, ChatRow(id=HOP, type="channel", title="Export", source_id="import:export"))
    _store(conn, SEED, 1, "forwarded", fwd_peer_id=HOP, fwd_msg_id=3)
    session = _start(rdb, conn)
    research.discover_offline(rdb, conn, CFG, session.id)
    (view,) = research.candidate_views(rdb, conn, session)
    assert view.cached_chats == (HOP,) and view.cached_accounts == ()


# --- depth, cursors and the cap --------------------------------------------------------------


def test_a_deeper_path_adds_evidence_to_an_existing_candidate(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    _store(conn, SEED, 1, "t.me/alpha_rent", links=(("link", "@alpha_rent"),))
    session = _start(rdb, conn, max_depth=1)
    research.discover_offline(rdb, conn, CFG, session.id)
    db.upsert_chat(conn, ChatRow(id=HOP, type="channel", title="Hop"))
    _store(conn, HOP, 4, "t.me/alpha_rent again", links=(("link", "@alpha_rent"),))
    _register(rdb, conn, session, HOP, 1)

    report = research.discover_offline(rdb, conn, CFG, session.id)

    (view,) = research.candidate_views(rdb, conn, session)
    assert view.candidate.depth == 1 and view.corroboration == 2
    assert report.updated_candidates == [view.candidate.id]


def test_discovery_resumes_from_its_cursor(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    _store(conn, SEED, 1, "t.me/alpha_rent", links=(("link", "@alpha_rent"),))
    session = _start(rdb, conn)
    research.discover_offline(rdb, conn, CFG, session.id)
    cursor = scan_cursor(rdb, session.id, ChatKey("", SEED))
    assert cursor is not None
    assert (cursor.depth, cursor.lead_seq, cursor.index_id) == (
        0,
        db.newest_lead_seq(conn, SEED),
        db.index_id(conn),
    )

    again = research.discover_offline(rdb, conn, CFG, session.id)
    assert (again.messages_scanned, again.leads, again.new_candidates) == (0, 0, [])

    _store(conn, SEED, 2, "t.me/beta_rent", links=(("link", "@beta_rent"),))
    later = research.discover_offline(rdb, conn, CFG, session.id)
    assert later.messages_scanned == 1 and len(later.new_candidates) == 1
    assert set(_by_identity(rdb, conn, session)) == {"@alpha_rent", "@beta_rent"}


def test_the_cap_keeps_the_best_corroborated_and_rereads_the_rest_next_time(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    _store(conn, SEED, 1, "t.me/weak_chan", links=(("link", "@weak_chan"),))
    for msg_id in (2, 3, 4):
        _store(conn, SEED, msg_id, f"t.me/strong_chan {msg_id}", links=(("link", "@strong_chan"),))
    session = _start(rdb, conn, max_candidates=1)

    first = research.discover_offline(rdb, conn, CFG, session.id)

    assert set(_by_identity(rdb, conn, session)) == {"@strong_chan"}
    assert first.over_cap == 1 and first.truncated
    assert scan_cursor(rdb, session.id, ChatKey("", SEED)) is None, "held back"

    second = research.discover_offline(rdb, conn, CFG, session.id)
    assert set(_by_identity(rdb, conn, session)) == {"@strong_chan", "@weak_chan"}
    assert not second.truncated and len(second.new_candidates) == 1
    assert len(_by_identity(rdb, conn, session)["@strong_chan"].evidence) == 3


def test_candidates_rank_by_corroboration_then_question_overlap(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    _store(conn, SEED, 1, "cats and dogs t.me/pets_chan", links=(("link", "@pets_chan"),))
    _store(conn, SEED, 2, "apartments in Tbilisi t.me/flats_chan", links=(("link", "@flats_chan"),))
    for msg_id in (3, 4):
        _store(conn, SEED, msg_id, f"news {msg_id}", links=(("link", "@news_chan"),))
    session = _start(rdb, conn)
    research.discover_offline(rdb, conn, CFG, session.id)

    views = research.candidate_views(rdb, conn, session)

    assert [v.candidate.identity for v in views] == ["@news_chan", "@flats_chan", "@pets_chan"]
    assert [(v.corroboration, v.overlap) for v in views] == [(2, 0), (1, 2), (1, 0)]


# --- rows stored before links were captured --------------------------------------------------


def test_rows_stored_before_capture_are_read_by_their_visible_text(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    _store(conn, SEED, 1, "old post: t.me/old_chan and @old_mention", links=None)
    _store(conn, SEED, 2, "old plain text", links=None)
    db.set_meta(conn, db.META_LINKS_CAPTURED_FROM, "100")
    _store(conn, SEED, 3, "new: t.me/new_chan", links=(("text_url", "@new_chan"),))
    session = _start(rdb, conn)

    report = research.discover_offline(rdb, conn, CFG, session.id)

    found = _by_identity(rdb, conn, session)
    assert {identity: v.evidence[0].via for identity, v in found.items()} == {
        "@old_chan": "link",
        "@old_mention": "mention",
        "@new_chan": "text_url",
    }
    assert report.text_fallback == 1


def test_a_captured_row_is_never_read_by_its_text(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    _store(conn, SEED, 1, "t.me/visible_only", links=())
    session = _start(rdb, conn)
    report = research.discover_offline(rdb, conn, CFG, session.id)
    assert research.candidate_views(rdb, conn, session) == []
    assert report.text_fallback == 0


# --- helpers ---------------------------------------------------------------------------------


def test_snippet_centres_on_the_target_in_a_long_text() -> None:
    text = "x " * 300 + "the @needle_chan is here " + "y " * 300
    piece = research.snippet(text, "needle_chan")
    assert piece is not None and "@needle_chan" in piece
    assert len(piece) <= research.SNIPPET_CHARS + 2
    assert piece.startswith("…") and piece.endswith("…")
    assert research.snippet("  a\n b  ") == "a b"
    assert research.snippet("   ") is None


def test_a_link_with_an_id_past_telegram_s_range_is_skipped_not_fatal(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    """A crafted link anyone can post — in the stored links or only in the text — never stops
    discovery: it names nothing, the other leads are read and the cursor moves past it."""
    _store(
        conn,
        SEED,
        1,
        "see https://t.me/c/99999999999999999999/5",
        links=(("link", "c/99999999999999999999/5"), ("link", "@alpha_rent")),
    )
    _store(
        conn,
        SEED,
        2,
        "tg://privatepost?channel=99999999999999999999&post=5 and t.me/c/99999999999999999999",
        links=None,
    )
    _store(conn, SEED, 3, "t.me/c/" + "9" * 5000 + "/1 or @beta_rent", links=None)
    session = _start(rdb, conn)

    report = research.discover_offline(rdb, conn, CFG, session.id)

    found = {c.identity for c in research_db.list_candidates(rdb, session.id)}
    assert found == {"@alpha_rent", "@beta_rent"}
    assert report.leads == 2
    again = research.discover_offline(rdb, conn, CFG, session.id)
    assert (again.messages_scanned, again.leads) == (0, 0), "the cursor moved past them"


def test_message_text_never_reaches_the_log_above_debug(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "the secret landlord phone 555"
    _store(conn, SEED, 1, f"{secret} t.me/alpha_rent", links=(("link", "@alpha_rent"),))
    session = _start(rdb, conn)
    with caplog.at_level(logging.INFO, logger="grepogram"):
        research.discover_offline(rdb, conn, CFG, session.id)
    assert caplog.records
    assert all(secret not in record.getMessage() for record in caplog.records)


# --- probing ---------------------------------------------------------------------------------

FLATS = make_channel(3001, "Tbilisi flats", username="tb_flats")
SECRET = make_channel(3002, "Secret rentals")
GATED = make_channel(3003, "Invite-only rent", megagroup=True)
MINE = make_channel(3004, "My group", megagroup=True)
PEEK = make_channel(3005, "Peekable")
FOLDER_CHAN = make_channel(3006, "Folder chan", username="folder_chan")
FOLDER_PRIVATE = make_channel(3007, "Folder private", megagroup=True)
BANNED_FROM = make_channel(3008, "Banned from", username="banned_from")
OLD_GROUP = make_group(3009, "Old group")
TOM = make_user(4001, "Tbilisi", "Tom", username="tbilisi_tom")
SEED_CHANNEL = make_channel(100, "Tbilisi rent", username="TbRent")


def _marked(entity: Any) -> int:
    return int(utils.get_peer_id(entity))


def _world(**kwargs: Any) -> FakeWorld:
    return FakeWorld(
        entities=[
            FLATS,
            SECRET,
            GATED,
            MINE,
            PEEK,
            FOLDER_CHAN,
            FOLDER_PRIVATE,
            BANNED_FROM,
            OLD_GROUP,
            TOM,
            SEED_CHANNEL,
        ],
        invites={
            "JoinMe": FakeInvite(GATED, request_needed=True, participants=812),
            "Already": FakeInvite(MINE),
            "PeekIn": FakeInvite(PEEK, peek=True),
            "Expired": errors.InviteHashExpiredError(request=None),
        },
        chatlists={
            "Tbilisi1": FakeChatlist(
                "Tbilisi housing", [FOLDER_CHAN, FOLDER_PRIVATE, TOM, SEED_CHANNEL, FLATS]
            ),
            "Gone": errors.BadRequestError(None, "INVITE_SLUG_EXPIRED", 400),
        },
        **kwargs,
    )


def _pinned(kwargs: dict[str, Any]) -> bool:
    return kwargs.get("filter") is types.InputMessagesFilterPinned


def _history_calls(client: FakeClient) -> list[str]:
    """History reads, the pinned posts of the session's own chats aside (:func:`_pin_reads`)."""
    return [
        name
        for name, kwargs in client.calls
        if name in ("iter_messages", "get_messages") and not _pinned(kwargs)
    ]


def _pin_reads(client: FakeClient) -> list[int]:
    """The chats whose pinned posts were read, in order."""
    return [
        int(kw["chat_id"]) for name, kw in client.calls if name == "iter_messages" and _pinned(kw)
    ]


def _counts(conn: sqlite3.Connection) -> tuple[int, int]:
    messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    chats = conn.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
    return int(messages), int(chats)


def _links(conn: sqlite3.Connection, *targets: str) -> None:
    for msg_id, target in enumerate(targets, start=1):
        _store(conn, SEED, msg_id, f"see {target}", links=(("link", target),))


async def test_a_username_probe_reads_metadata_and_no_history(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    _links(conn, "@tb_flats")
    session = _start(rdb, conn)
    research.discover_offline(rdb, conn, CFG, session.id)
    client = _world().client("default")

    report = await research.probe_candidates(client, rdb, conn, CFG, session.id, now=9)

    (view,) = research.candidate_views(rdb, conn, session)
    candidate = view.candidate
    assert report.probed == [candidate.id] and report.remaining == 0
    assert (candidate.title, candidate.type, candidate.member) == (
        "Tbilisi flats",
        "channel",
        False,
    )
    assert candidate.peer_id == _marked(FLATS)
    assert candidate.access_hash == FakeWorld.access_hash("default", _marked(FLATS))
    assert candidate.probed_at == 9 and candidate.status == "proposed"
    assert not view.cached, "probing learns what a chat is; it never caches it"
    assert _history_calls(client) == []


async def test_membership_is_the_acting_account_s(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    """``member`` is what Telegram tells the session's own account: work being in the chat says
    nothing about default, and the other way round."""
    _links(conn, "@tb_flats")
    world = _world()
    as_default = _start(rdb, conn)
    as_work = research.start_session(rdb, conn, WORK_CFG, QUESTION, [str(SEED)], "work", now=1)
    for session, cfg in ((as_default, CFG), (as_work, WORK_CFG)):
        research.discover_offline(rdb, conn, cfg, session.id)

    await research.probe_candidates(world.client("default"), rdb, conn, CFG, as_default.id)
    work = world.client("work", members=[FLATS])
    await research.probe_candidates(work, rdb, conn, WORK_CFG, as_work.id)

    (for_default,) = research_db.list_candidates(rdb, as_default.id)
    (for_work,) = research_db.list_candidates(rdb, as_work.id)
    assert for_default.member is False, "work's membership is not default's"
    assert for_work.member is True


async def test_a_username_nobody_holds_or_a_user_is_recorded_as_such(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    _links(conn, "@nobody_here", "@tbilisi_tom", "@banned_from")
    session = _start(rdb, conn)
    research.discover_offline(rdb, conn, CFG, session.id)
    banned = errors.ChannelPrivateError(request=None)
    client = _world().client("default", entity_errors={"@banned_from": banned})

    report = await research.probe_candidates(client, rdb, conn, CFG, session.id)

    found = {c.identity: c for c in research_db.list_candidates(rdb, session.id)}
    assert found["@nobody_here"].status == "unavailable"
    assert "no chat or user holds @nobody_here" in (found["@nobody_here"].note or "")
    assert found["@tbilisi_tom"].type == "user" and found["@tbilisi_tom"].status == "proposed"
    assert "not a group or channel" in (found["@tbilisi_tom"].note or "")
    assert found["@banned_from"].status == "unavailable"
    assert (found["@banned_from"].note or "").startswith("Telegram refused")
    assert sorted(report.unavailable) == sorted(
        [found["@nobody_here"].id, found["@banned_from"].id]
    )


async def test_every_invite_shape_is_recorded_for_what_it_says(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    _links(conn, "+JoinMe", "+Already", "+PeekIn", "+Expired", "+NoSuchHash")
    session = _start(rdb, conn)
    research.discover_offline(rdb, conn, CFG, session.id)
    client = _world().client("default", members=[MINE])

    report = await research.probe_candidates(client, rdb, conn, CFG, session.id)

    found = {c.identity: c for c in research_db.list_candidates(rdb, session.id)}
    gated = found["+JoinMe"]
    assert (gated.title, gated.type, gated.participants) == ("Invite-only rent", "supergroup", 812)
    assert gated.request_needed is True and gated.member is False
    assert gated.peer_id is None, "a chatInvite names no peer, and none is guessed"
    already = found["+Already"]
    assert already.member is True and already.peer_id == _marked(MINE)
    assert already.access_hash == FakeWorld.access_hash("default", _marked(MINE))
    peek = found["+PeekIn"]
    assert peek.member is False and peek.peer_id == _marked(PEEK)
    assert "previewable without joining" in (peek.note or "")
    for dead in ("+Expired", "+NoSuchHash"):
        assert found[dead].status == "unavailable"
        assert (found[dead].note or "").startswith("Telegram refused")
    assert len(report.probed) == 3 and len(report.unavailable) == 2
    sent = [type(r) for r in client.requests]
    assert sent == [functions.messages.CheckChatInviteRequest] * 5
    assert _history_calls(client) == []


async def test_a_shared_folder_s_chats_become_candidates_of_their_own(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    _links(conn, "addlist/Tbilisi1")
    session = _start(rdb, conn)
    research.discover_offline(rdb, conn, CFG, session.id)
    research_db.add_exclusion(rdb, "@tb_flats")
    client = _world().client("default")

    report = await research.probe_candidates(client, rdb, conn, CFG, session.id, now=4)

    found = {c.identity: c for c in research_db.list_candidates(rdb, session.id)}
    folder = found["addlist/Tbilisi1"]
    assert folder.title == "Tbilisi housing" and folder.member is False
    public = found["@folder_chan"]
    private = found[f"peer:{_marked(FOLDER_PRIVATE)}"]
    assert set(found) == {"addlist/Tbilisi1", "@folder_chan", f"peer:{_marked(FOLDER_PRIVATE)}"}
    assert sorted(report.children) == sorted([public.id, private.id])
    # the folder's person, the seed it lists and the excluded chat are counted, not proposed
    assert (report.people, report.in_session, report.excluded, report.over_cap) == (1, 1, 1, 0)
    for child in (public, private):
        assert child.parent_id == folder.id and child.depth == folder.depth
        assert child.status == "proposed" and child.probed_at == 4 and child.member is False
        (evidence,) = research_db.list_evidence(rdb, child.id)
        assert (evidence.via, evidence.origin_key) == ("shared_folder", "addlist:Tbilisi1")
        assert evidence.snippet == "Tbilisi housing"
        assert research_db.live_grants(rdb, session.id, child.id) == []
    assert private.access_hash == FakeWorld.access_hash("default", _marked(FOLDER_PRIVATE))
    assert private.type == "supergroup"


async def test_an_imported_folder_says_which_of_its_chats_are_joined(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    _links(conn, "addlist/Tbilisi1")
    session = _start(rdb, conn)
    research.discover_offline(rdb, conn, CFG, session.id)
    client = _world().client("default", members=[FOLDER_CHAN], chatlists_joined={"Tbilisi1"})

    await research.probe_candidates(client, rdb, conn, CFG, session.id)

    found = {c.identity: c for c in research_db.list_candidates(rdb, session.id)}
    assert found["addlist/Tbilisi1"].member is True
    assert found["@folder_chan"].member is True
    assert found[f"peer:{_marked(FOLDER_PRIVATE)}"].member is False
    assert found["@tb_flats"].member is False


async def test_a_dead_folder_link_is_unavailable(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    _links(conn, "addlist/Gone")
    session = _start(rdb, conn)
    research.discover_offline(rdb, conn, CFG, session.id)

    await research.probe_candidates(_world().client("default"), rdb, conn, CFG, session.id)

    (folder,) = research_db.list_candidates(rdb, session.id)
    assert folder.status == "unavailable" and "INVITE_SLUG_EXPIRED" in (folder.note or "")


async def test_a_private_forward_origin_is_unresolvable_and_nothing_is_asked(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    _store(conn, SEED, 1, "flat", fwd_from="Secret", fwd_peer_id=_marked(SECRET), fwd_msg_id=3)
    session = _start(rdb, conn)
    research.discover_offline(rdb, conn, CFG, session.id)
    client = _world().client("default")

    report = await research.probe_candidates(client, rdb, conn, CFG, session.id)

    (candidate,) = research_db.list_candidates(rdb, session.id)
    assert report.unresolvable == [candidate.id]
    assert candidate.status == "proposed" and candidate.note == research.UNRESOLVABLE_NOTE
    assert (candidate.title, candidate.type, candidate.member) == (None, None, None)
    assert client.requests == [] and client.calls == []


async def test_a_peer_the_index_holds_a_hash_for_is_looked_up_with_it(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    secret = _marked(SECRET)
    db.upsert_chat(conn, ChatRow(id=secret, type="channel", title="Secret rentals"))
    db.set_chat_access(
        conn, secret, "default", access_hash=FakeWorld.access_hash("default", secret)
    )
    _store(conn, SEED, 1, "flat", fwd_from="Secret", fwd_peer_id=secret, fwd_msg_id=3)
    session = _start(rdb, conn)
    research.discover_offline(rdb, conn, CFG, session.id)
    client = _world().client("default", members=[SECRET])

    report = await research.probe_candidates(client, rdb, conn, CFG, session.id)

    (candidate,) = research_db.list_candidates(rdb, session.id)
    assert report.probed == [candidate.id]
    assert candidate.title == "Secret rentals" and candidate.member is True
    (request,) = client.requests
    assert isinstance(request, functions.channels.GetChannelsRequest)


async def test_a_chat_the_account_was_banned_from_is_unavailable(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    banned = _marked(BANNED_FROM)
    db.upsert_chat(conn, ChatRow(id=banned, type="channel", title="Banned from"))
    db.set_chat_access(conn, banned, "default", access_hash=77)
    _store(conn, SEED, 1, "see", links=(("link", f"peer:{banned}"),))
    session = _start(rdb, conn)
    research.discover_offline(rdb, conn, CFG, session.id)
    forbidden = types.ChannelForbidden(id=3008, access_hash=77, title="Banned from")
    client = _world().client(
        "default",
        responses={functions.channels.GetChannelsRequest: types.messages.Chats([forbidden])},
    )

    report = await research.probe_candidates(client, rdb, conn, CFG, session.id)

    (candidate,) = research_db.list_candidates(rdb, session.id)
    assert report.unavailable == [candidate.id]
    assert candidate.status == "unavailable" and candidate.member is False
    assert "banned or removed" in (candidate.note or "")
    (request,) = client.requests
    assert request.id[0].access_hash == 77, "the hash this account stored, nothing else"


async def test_a_legacy_group_peer_is_probed_only_by_a_member(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    _store(conn, SEED, 1, "see", links=(("link", f"peer:{_marked(OLD_GROUP)}"),))
    session = _start(rdb, conn)
    research.discover_offline(rdb, conn, CFG, session.id)

    await research.probe_candidates(_world().client("default"), rdb, conn, CFG, session.id)
    (outsider,) = research_db.list_candidates(rdb, session.id)
    assert outsider.status == "unavailable"

    member = _start(rdb, conn)
    research.discover_offline(rdb, conn, CFG, member.id)
    client = _world().client("default", members=[OLD_GROUP])
    await research.probe_candidates(client, rdb, conn, CFG, member.id)
    (candidate,) = research_db.list_candidates(rdb, member.id)
    assert (candidate.title, candidate.type, candidate.member) == ("Old group", "group", True)
    assert candidate.access_hash is None, "a legacy group needs no access hash"


def test_a_hidden_forward_origin_names_nothing(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    seed = db.get_chat(conn, SEED)
    assert seed is not None
    hidden = tl.forwarded_message(SEED, 1, "flat for rent", origin_name="Someone Hidden")
    row = sync.map_message(hidden, seed, {})
    assert row is not None and row.fwd_peer_id is None and row.fwd_msg_id is None
    db.upsert_messages(conn, [row])
    session = _start(rdb, conn)

    report = research.discover_offline(rdb, conn, CFG, session.id)

    assert report.leads == 0 and research_db.list_candidates(rdb, session.id) == []


async def test_a_flood_wait_stops_probing_with_a_warning(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    _links(conn, "@tb_flats", "@folder_chan")
    session = _start(rdb, conn)
    research.discover_offline(rdb, conn, CFG, session.id)
    flood = errors.FloodWaitError(request=None, capture=300)
    client = _world().client("default", entity_errors={"@tb_flats": flood})

    report = await research.probe_candidates(client, rdb, conn, CFG, session.id)

    assert report.flood_wait_s == 300 and report.probed == [] and report.remaining == 2
    assert "300s" in report.warnings[0]
    assert [key for name, kw in client.calls if name == "get_entity" for key in [kw["key"]]] == [
        "@tb_flats"
    ]


async def test_probe_limit_bounds_one_call_and_the_next_goes_on(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    _links(conn, "@tb_flats", "@folder_chan", "@nobody_here")
    session = _start(rdb, conn, probe_limit=2)
    research.discover_offline(rdb, conn, CFG, session.id)
    client = _world().client("default")

    first = await research.probe_candidates(client, rdb, conn, CFG, session.id)
    second = await research.probe_candidates(client, rdb, conn, CFG, session.id)

    assert len(first.probed) + len(first.unavailable) == 2 and first.remaining == 1
    assert len(second.probed) + len(second.unavailable) == 1 and second.remaining == 0


async def test_probing_refuses_while_disabled_or_stopped(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    session = _start(rdb, conn)
    client = _world().client("default")
    with pytest.raises(research.ResearchDisabled):
        await research.probe_candidates(client, rdb, conn, Config(), session.id)
    research_db.stop_session(rdb, session.id)
    with pytest.raises(research.SessionStopped):
        await research.probe_candidates(client, rdb, conn, CFG, session.id)
    assert client.calls == []


async def test_a_dead_session_while_probing_names_the_account(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    _links(conn, "+JoinMe")
    session = _start(rdb, conn)
    research.discover_offline(rdb, conn, CFG, session.id)
    dead = errors.AuthKeyUnregisteredError(request=None)
    client = _world().client("default", responses={functions.messages.CheckChatInviteRequest: dead})
    with pytest.raises(tg.AuthRequired) as caught:
        await research.probe_candidates(client, rdb, conn, CFG, session.id)
    assert caught.value.account == "default"


# --- global search ---------------------------------------------------------------------------

SEARCH_CFG = Config(research=ResearchCfg(enabled=True, chat_search=True, post_search=True))


def _grant(rdb: sqlite3.Connection, session: ResearchSession, *actions: Any) -> None:
    research_db.add_grant(
        rdb,
        session_id=session.id,
        candidate_id=None,
        account=session.account,
        actions=list(actions),
        via="cli",
        summary="search Telegram for the question",
    )


def _posts_world(**kwargs: Any) -> FakeWorld:
    posts = [
        tl.message(_marked(FLATS), 11, "Apartment in Vake for rent", date=tl.at(10)),
        tl.message(_marked(FLATS), 12, "nothing relevant", date=tl.at(11)),
        tl.message(_marked(FOLDER_CHAN), 5, "Large apartment near the park", date=tl.at(12)),
        tl.message(_marked(SECRET), 7, "Apartment, private channel", date=tl.at(13)),
    ]
    by_chat: dict[int, list[types.Message]] = {}
    for post in posts:
        by_chat.setdefault(int(utils.get_peer_id(post.peer_id)), []).append(post)
    return _world(messages=by_chat, **kwargs)


async def test_global_search_needs_the_switch_and_a_grant(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    session = _asking(rdb, conn, "tbilisi")
    client = _world().client("default")
    with pytest.raises(research.ResearchError) as off:
        await research.global_search(client, rdb, conn, CFG, session.id, "tbilisi")
    assert "chat_search = true" in (off.value.hint or "")
    with pytest.raises(research.ResearchError) as ungranted:
        await research.global_search(client, rdb, conn, SEARCH_CFG, session.id, "tbilisi")
    assert "approve" in (ungranted.value.hint or "")
    only_chats = Config(research=ResearchCfg(enabled=True, chat_search=True))
    _grant(rdb, session, "global_search")
    with pytest.raises(research.ResearchError):
        await research.global_search(
            client, rdb, conn, only_chats, session.id, "tbilisi", kinds=["post_search"]
        )
    assert client.requests == [] and research_db.list_searches(rdb, session.id) == []


async def test_chat_search_proposes_public_chats_as_evidence_only(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    session = _asking(rdb, conn, "tbilisi")
    _grant(rdb, session, "global_search")
    client = _world().client("default")
    before = _counts(conn)

    (report,) = await research.global_search(
        client, rdb, conn, SEARCH_CFG, session.id, "tbilisi", kinds=["chat_search"], now=6
    )

    assert _counts(conn) == before, "search results never become index rows"
    found = {c.identity: c for c in research_db.list_candidates(rdb, session.id)}
    assert set(found) == {"@tb_flats"}
    flats = found["@tb_flats"]
    assert flats.depth == 1 and flats.probed_at == 6 and flats.title == "Tbilisi flats"
    (evidence,) = research_db.list_evidence(rdb, flats.id)
    assert (evidence.via, evidence.origin_key) == ("chat_search", f"chat_search:{_marked(FLATS)}")
    assert report.ran and report.new_candidates == [flats.id]
    assert report.people == 1, "a user matching the query is a person, not a candidate"
    assert report.in_session == 1, "the seed channel the session reads is not proposed"
    (record,) = research_db.list_searches(rdb, session.id)
    assert (record.kind, record.query, record.results) == ("chat_search", "tbilisi", 3)


async def test_post_search_asks_the_quota_first_and_records_posts_as_evidence(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    session = _asking(rdb, conn, "apartment")
    _grant(rdb, session, "global_search")
    client = _posts_world().client("default")
    before = _counts(conn)

    (report,) = await research.global_search(
        client, rdb, conn, SEARCH_CFG, session.id, "apartment", kinds=["post_search"]
    )

    assert _counts(conn) == before
    check, search = client.requests
    assert isinstance(check, functions.channels.CheckSearchPostsFloodRequest)
    assert isinstance(search, functions.channels.SearchPostsRequest)
    assert search.allow_paid_stars is None and search.query == "apartment"
    assert (report.quota_total, report.quota_remains, report.paid_stars) == (10, 10, 0)
    found = {c.identity: c for c in research_db.list_candidates(rdb, session.id)}
    assert set(found) == {"@tb_flats", "@folder_chan"}, "a private channel's posts are not public"
    (evidence,) = research_db.list_evidence(rdb, found["@tb_flats"].id)
    assert evidence.via == "post_search"
    assert (evidence.chat, evidence.msg_id) == (ChatKey("", _marked(FLATS)), 11)
    assert evidence.origin_key == f"post:{_marked(FLATS)}/11"
    assert evidence.snippet == "Apartment in Vake for rent"
    assert _history_calls(client) == []


async def test_a_spent_quota_is_never_paid_for_by_default(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    session = _asking(rdb, conn, "apartment")
    _grant(rdb, session, "global_search", "paid_search")
    spent = types.SearchPostsFlood(total_daily=10, remains=0, stars_amount=50, wait_till=99)
    client = _posts_world().client("default", search_flood=spent)

    (report,) = await research.global_search(
        client, rdb, conn, SEARCH_CFG, session.id, "apartment", kinds=["post_search"]
    )

    assert not report.ran and report.wait_till == 99
    assert "paid_stars_max = 0" in report.warnings[0]
    assert [type(r) for r in client.requests] == [functions.channels.CheckSearchPostsFloodRequest]
    assert research_db.list_candidates(rdb, session.id) == []
    (record,) = research_db.list_searches(rdb, session.id)
    assert record.results == 0 and "paid_stars_max" in (record.note or "")


async def test_paying_needs_the_ceiling_and_a_separate_paid_grant_used_once(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    session = _asking(rdb, conn, "apartment")
    _grant(rdb, session, "global_search")
    spent = types.SearchPostsFlood(total_daily=10, remains=0, stars_amount=50)
    paying = Config(research=ResearchCfg(enabled=True, post_search=True, paid_stars_max=100))
    cheap = Config(research=ResearchCfg(enabled=True, post_search=True, paid_stars_max=20))
    client = _posts_world().client("default", search_flood=spent)

    (no_grant,) = await research.global_search(client, rdb, conn, paying, session.id, "apartment")
    assert not no_grant.ran and "paid_search approval" in no_grant.warnings[0]
    _grant(rdb, session, "paid_search")
    (too_dear,) = await research.global_search(client, rdb, conn, cheap, session.id, "apartment")
    assert not too_dear.ran and "above paid_stars_max = 20" in too_dear.warnings[0]
    assert not any(isinstance(r, functions.channels.SearchPostsRequest) for r in client.requests)

    (paid,) = await research.global_search(client, rdb, conn, paying, session.id, "apartment")

    assert paid.ran and paid.paid_stars == 50 and len(paid.new_candidates) == 2
    (search,) = [r for r in client.requests if isinstance(r, functions.channels.SearchPostsRequest)]
    assert search.allow_paid_stars == 50
    assert not research.search_granted(rdb, session.id, "paid_search"), "a paid grant pays once"
    assert research.search_granted(rdb, session.id, "global_search")
    (again,) = await research.global_search(client, rdb, conn, paying, session.id, "apartment")
    assert not again.ran and "paid_search approval" in again.warnings[0]


async def test_a_flood_wait_on_search_is_recorded_and_stops_the_searches(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    session = _asking(rdb, conn, "tbilisi")
    _grant(rdb, session, "global_search")
    flood = errors.FloodWaitError(request=None, capture=60)
    client = _world().client("default", responses={functions.contacts.SearchRequest: flood})

    reports = await research.global_search(client, rdb, conn, SEARCH_CFG, session.id, "tbilisi")

    (report,) = reports
    assert report.kind == "chat_search" and report.flood_wait_s == 60 and not report.ran
    assert not any(isinstance(r, functions.channels.SearchPostsRequest) for r in client.requests)
    (record,) = research_db.list_searches(rdb, session.id)
    assert "wait 60s" in (record.note or "")


async def test_an_excluded_search_result_is_never_proposed(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    session = _asking(rdb, conn, "flats")
    _grant(rdb, session, "global_search")
    research_db.add_exclusion(rdb, "@tb_flats")

    (report,) = await research.global_search(
        _world().client("default"),
        rdb,
        conn,
        SEARCH_CFG,
        session.id,
        "flats",
        kinds=["chat_search"],
    )

    assert report.excluded == 1 and research_db.list_candidates(rdb, session.id) == []


# --- discover --------------------------------------------------------------------------------


async def test_discover_composes_offline_leads_search_and_probing(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    _links(conn, "+JoinMe")
    session = _start(rdb, conn)
    _grant(rdb, session, "global_search")
    client = _posts_world().client("default")
    before = _counts(conn)

    report = await research.discover(rdb, conn, SEARCH_CFG, session.id, client)

    assert _counts(conn) == before
    assert len(report.new_candidates) == 1
    assert [s.kind for s in report.searches] == ["chat_search", "post_search"]
    assert report.searches[0].query == QUESTION
    assert report.probe is not None and len(report.probe.probed) == 1
    gated = research_db.candidate_by_identity(rdb, session.id, "+JoinMe")
    assert gated is not None and gated.request_needed is True

    again = await research.discover(rdb, conn, SEARCH_CFG, session.id, client)

    assert again.searches == (), "a question already searched is not sent again"
    assert len(research_db.list_searches(rdb, session.id)) == 2


async def test_discover_without_a_grant_or_a_client_never_searches(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    _links(conn, "@tb_flats")
    session = _start(rdb, conn)
    offline = await research.discover(rdb, conn, SEARCH_CFG, session.id)
    assert offline.probe is None and offline.searches == ()
    client = _world().client("default")

    report = await research.discover(rdb, conn, SEARCH_CFG, session.id, client)

    assert report.searches == () and report.probe is not None
    assert not any(
        isinstance(r, functions.contacts.SearchRequest | functions.channels.SearchPostsRequest)
        for r in client.requests
    )


# --- approval --------------------------------------------------------------------------------

PAID_CFG = Config(
    research=ResearchCfg(enabled=True, chat_search=True, post_search=True, paid_stars_max=5)
)


def _probed(
    rdb: sqlite3.Connection,
    session: ResearchSession,
    identity: str,
    kind: Any = "username",
    *,
    depth: int = 1,
    parent_id: int | None = None,
    **facts: Any,
) -> Candidate:
    """A candidate as a probe left it: ``facts`` are what Telegram answered."""
    target = leads.normalize(identity)
    assert target is not None
    added = research_db.add_candidate(
        rdb,
        session.id,
        identity,
        kind,
        depth,
        peer_id=target.peer_id,
        username=target.username,
        invite_hash=target.invite_hash,
        addlist_slug=target.slug,
        parent_id=parent_id,
        now=1,
    )
    assert added is not None
    return research_db.update_candidate(rdb, added.id, **{"probed_at": 2, **facts})


def _flats(rdb: sqlite3.Connection, session: ResearchSession, **facts: Any) -> Candidate:
    known = {"title": "Tbilisi flats", "type": "channel", "participants": 1234, "member": False}
    return _probed(rdb, session, "@tb_flats", **{**known, **facts})


def _item(candidate: Candidate | None, *actions: str) -> ApprovalItem:
    return ApprovalItem(candidate_id=None if candidate is None else candidate.id, actions=actions)


def _approve(
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    session: ResearchSession,
    *items: ApprovalItem,
    cfg: Config = CFG,
) -> list[Grant]:
    summary = research.approval_summary(rdb, conn, cfg, session.id, items)
    return research.grant(rdb, conn, cfg, session.id, items, via="cli", summary=summary, now=5)


def test_the_summary_names_target_account_membership_and_every_action(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    session = _start(rdb, conn)
    flats = _flats(rdb, session)

    text = research.approval_summary(
        rdb, conn, CFG, session.id, [_item(flats, "add_source", "fetch", "join")]
    )

    assert text == "\n".join(
        [
            f'Research session {session.id}: "{QUESTION}"',
            "Acting account: default",
            "",
            f'Candidate {flats.id}: "Tbilisi flats" (@tb_flats), channel, 1,234 members',
            "  account default is not a member; not in the index yet",
            "  - join it as default through its public username @tb_flats; the account becomes "
            "a member, visible to its admins",
            "  - fetch its history since 1969-01-01 with the comments of its discussion group as "
            "default into the local index",
            "  - add it as an ongoing source of account default (since 1969-01-01, with the "
            "comments of its discussion group): regular sync and search will include it from "
            "now on, and stopping this research session does not remove it",
            "",
            research.DESCENDANTS_NOTE,
        ]
    )
    assert research_db.list_grants(rdb, session.id) == [], "a summary grants nothing"
    assert research_db.get_candidate(rdb, flats.id).status == "proposed"  # type: ignore[union-attr]


def test_the_summary_says_what_the_index_holds_and_how_a_request_works(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    session = _start(rdb, conn)
    cached = _probed(rdb, session, "@cachedchan", title="Already here", type="channel", member=True)
    gated = _probed(
        rdb,
        session,
        "+JoinMe",
        "invite",
        title="Gated",
        type="supergroup",
        participants=812,
        member=False,
        request_needed=True,
    )

    text = research.approval_summary(
        rdb,
        conn,
        CFG,
        session.id,
        [_item(cached, "add_source"), _item(gated, "request", "fetch", "add_source")],
    )

    assert "account default is a member; already in the index through work" in text
    assert (
        f'Candidate {gated.id}: "Gated" (invite link t.me/+JoinMe), supergroup, 812 members, '
        "its admins approve who joins"
    ) in text
    assert "send a request to join it as default; its admins see the request and decide" in text
    assert "discussion group" not in text.split(f"Candidate {gated.id}")[1]


def test_a_search_approval_discloses_where_the_query_goes_and_what_it_pays(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    session = _start(rdb, conn)

    text = research.approval_summary(
        rdb, conn, PAID_CFG, session.id, [_item(None, "paid_search", "global_search")]
    )

    assert f'send this session\'s question "{QUESTION}" as default to' in text
    assert "contacts.search" in text and "channels.searchPosts" in text
    assert "the query leaves this computer and reaches Telegram" in text
    assert "snippets from channels and groups you have never joined or indexed" in text
    assert "pay up to 5 Telegram Stars from default's balance" in text
    assert text.index("send this") < text.index("pay up to")
    only_chats = Config(research=ResearchCfg(enabled=True, chat_search=True))
    chats_text = research.approval_summary(
        rdb, conn, only_chats, session.id, [_item(None, "global_search")]
    )
    assert "contacts.search" in chats_text and "searchPosts" not in chats_text


def test_grant_records_the_channel_and_the_text_the_human_saw(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    session = _start(rdb, conn)
    flats = _flats(rdb, session)
    items = [_item(flats, "join", "fetch", "add_source")]
    summary = research.approval_summary(rdb, conn, CFG, session.id, items)

    (granted,) = research.grant(
        rdb, conn, CFG, session.id, items, via="elicitation", summary=summary, now=5
    )

    assert (granted.via, granted.summary, granted.account) == ("elicitation", summary, "default")
    assert granted.actions == ("join", "fetch", "add_source") and granted.candidate_id == flats.id
    stored = research_db.get_candidate(rdb, flats.id)
    assert stored is not None and stored.status == "approved"
    assert all(research.authorized(rdb, stored, a) for a in ("join", "fetch", "add_source"))
    assert not research.authorized(rdb, stored, "request")


def test_one_approval_grants_each_named_target_and_nothing_else(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    """A batch is one summary and one answer for several named targets: each gets its own grant
    for exactly its own actions, and a candidate the batch did not name stays unauthorized."""
    session = _start(rdb, conn)
    flats = _flats(rdb, session)
    rooms = _probed(rdb, session, "@tb_rooms", title="Rooms", type="channel", member=True)
    unnamed = _probed(rdb, session, "@tb_other", title="Other", type="channel", member=False)
    items = [_item(flats, "join", "fetch", "add_source"), _item(rooms, "add_source")]
    summary = research.approval_summary(rdb, conn, CFG, session.id, items)
    assert f"Candidate {flats.id}:" in summary and f"Candidate {rooms.id}:" in summary
    assert f"Candidate {unnamed.id}:" not in summary

    granted = research.grant(rdb, conn, CFG, session.id, items, via="cli", summary=summary)

    assert [(g.candidate_id, g.actions, g.summary) for g in granted] == [
        (flats.id, ("join", "fetch", "add_source"), summary),
        (rooms.id, ("add_source",), summary),
    ]
    stored = {c.id: c for c in research_db.list_candidates(rdb, session.id)}
    assert research.authorized_actions(rdb, stored[flats.id]) == ["join", "fetch", "add_source"]
    assert research.authorized_actions(rdb, stored[rooms.id]) == ["add_source"]
    assert research.authorized_actions(rdb, stored[unnamed.id]) == []
    assert stored[unnamed.id].status == "proposed"


def test_a_summary_that_no_longer_matches_grants_nothing(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    session = _start(rdb, conn)
    flats = _flats(rdb, session)
    items = [_item(flats, "join", "fetch", "add_source")]
    summary = research.approval_summary(rdb, conn, CFG, session.id, items)
    research_db.update_candidate(rdb, flats.id, title="Tbilisi flats and more", participants=9)

    with pytest.raises(research.ResearchError, match="changed since its summary was shown"):
        research.grant(rdb, conn, CFG, session.id, items, via="cli", summary=summary)
    with pytest.raises(research.ResearchError):
        research.grant(rdb, conn, CFG, session.id, items, via="cli", summary="yes")
    fresh = research.approval_summary(rdb, conn, CFG, session.id, items)
    with pytest.raises(ValueError, match="grant channel"):
        research.grant(rdb, conn, CFG, session.id, items, via="mcp", summary=fresh)  # type: ignore[arg-type]
    assert research_db.list_grants(rdb, session.id) == []
    assert research_db.get_candidate(rdb, flats.id).status == "proposed"  # type: ignore[union-attr]


def test_invalid_action_combinations_are_refused(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    session = _start(rdb, conn)
    other = _start(rdb, conn)
    member = _flats(rdb, session, member=True)
    gated = _probed(rdb, session, "+JoinMe", "invite", type="supergroup", request_needed=True)
    open_invite = _probed(rdb, session, "+OpenDoor", "invite", type="supergroup", member=False)
    private = _probed(
        rdb, session, f"peer:{ORIGIN}", "peer", type="channel", member=False, access_hash=77
    )
    old_group = _probed(rdb, session, "peer:-4242", "peer", type="group", member=False)
    folder = _probed(rdb, session, "addlist/Tbilisi1", "addlist", title="Tbilisi housing")
    unprobed = research_db.add_candidate(rdb, session.id, "@tb_new", "username", 1)
    person = _probed(rdb, session, "@tom_rents", type="user")
    excluded = _probed(rdb, session, "@tb_banned", type="channel", status="excluded")
    unavailable = _probed(rdb, session, "@tb_gone", type="channel", status="unavailable")
    fetched = _probed(rdb, session, "@tb_done", type="channel", status="fetched")
    waiting = _probed(
        rdb,
        session,
        "+Waiting",
        "invite",
        type="supergroup",
        request_needed=True,
        status="pending_admission",
    )
    elsewhere = _flats(rdb, other)
    assert unprobed is not None

    cases: list[tuple[ApprovalItem, str]] = [
        (_item(member, "join"), "already a member"),
        (_item(member, "fetch"), "approve `add_source` together with `fetch`"),
        (_item(open_invite, "request"), "only for a chat whose invite asks"),
        (_item(gated, "join"), "approve `request` instead of `join`"),
        (_item(private, "fetch", "add_source"), "not a member of this private chat"),
        (_item(old_group, "join"), "no way to join it"),
        (_item(folder, "join"), "approved chat by chat"),
        (_item(unprobed, "join"), "not probed yet"),
        (_item(person, "fetch", "add_source"), "a person's account"),
        (_item(excluded, "fetch", "add_source"), "it is excluded"),
        (_item(unavailable, "join"), "Telegram refused it"),
        (_item(fetched, "add_source"), "already fetched it"),
        (_item(waiting, "request"), "already waiting"),
        (_item(member, "delete"), "unknown action 'delete'"),
        (_item(member, "global_search"), "approved for the session"),
        (_item(elsewhere, "add_source"), f"no candidate {elsewhere.id} in research session"),
        (_item(None, "fetch"), "approved for a candidate"),
        (_item(None, "global_search"), "global search is off"),
        (_item(member), "no action named"),
    ]
    for item, message in cases:
        with pytest.raises(research.ResearchError, match=re.escape(message)):
            research.approval_summary(rdb, conn, CFG, session.id, [item])
    with pytest.raises(research.ResearchError, match="nothing to approve"):
        research.approval_summary(rdb, conn, CFG, session.id, [])
    searches = Config(research=ResearchCfg(enabled=True, post_search=True))
    with pytest.raises(research.ResearchError, match=re.escape("paid_stars_max = 0")):
        research.approval_summary(
            rdb, conn, searches, session.id, [_item(None, "global_search", "paid_search")]
        )
    with pytest.raises(research.ResearchError, match="needs `global_search` approved too"):
        research.approval_summary(rdb, conn, PAID_CFG, session.id, [_item(None, "paid_search")])
    # a waiting request may still take what the run does once the admins let the account in
    assert "fetch its history" in research.approval_summary(
        rdb, conn, CFG, session.id, [_item(waiting, "fetch", "add_source")]
    )
    # join granted, then a new probe says the chat now asks for a request
    _approve(rdb, conn, session, _item(open_invite, "join"))
    research_db.update_candidate(rdb, open_invite.id, request_needed=True)
    with pytest.raises(research.ResearchError, match="two ways in"):
        research.approval_summary(rdb, conn, CFG, session.id, [_item(open_invite, "request")])
    assert [g.candidate_id for g in research_db.list_grants(rdb, session.id)] == [open_invite.id]


def test_descendants_of_an_approved_directory_stay_unauthorized(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    db.upsert_chat(conn, ChatRow(id=HOP, type="channel", title="Directory", username="tb_dir"))
    _store(conn, HOP, 1, "listing: @tb_deep", links=(("mention", "@tb_deep"),))
    session = _start(rdb, conn)
    directory = _probed(rdb, session, "@tb_dir", title="Directory", type="channel", member=False)
    _approve(rdb, conn, session, _item(directory, "join", "fetch", "add_source"))
    # the run fetched the directory: discovery now reads it one hop further out
    _register(rdb, conn, session, HOP, 1)
    research.discover_offline(rdb, conn, CFG, session.id)

    child = _by_identity(rdb, conn, session)["@tb_deep"].candidate
    assert child.depth == 2 and child.status == "proposed"
    assert research.authorized(rdb, directory, "fetch")
    for action in ("join", "request", "fetch", "add_source"):
        assert not research.authorized(rdb, child, action)
    assert research_db.live_grants(rdb, session.id, child.id) == []


def test_a_shared_folder_grants_nothing_for_its_chats(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    session = _start(rdb, conn)
    folder = _probed(rdb, session, "addlist/Tbilisi1", "addlist", title="Tbilisi housing")
    inside = _probed(
        rdb,
        session,
        f"peer:{ORIGIN}",
        "peer",
        parent_id=folder.id,
        title="Folder private",
        type="channel",
        member=False,
    )
    sibling = _probed(rdb, session, "@tb_folder_chan", parent_id=folder.id, type="channel")

    with pytest.raises(research.ResearchError, match="approved chat by chat"):
        research.approval_summary(rdb, conn, CFG, session.id, [_item(folder, "join")])
    items = [_item(inside, "join", "fetch", "add_source")]
    text = research.approval_summary(rdb, conn, CFG, session.id, items)
    assert (
        'through the shared folder "Tbilisi housing" (t.me/addlist/Tbilisi1); Telegram also adds '
        "that folder to the account's chat folders"
    ) in text
    _approve(rdb, conn, session, *items)

    assert research.authorized(rdb, inside, "join")
    assert not any(
        research.authorized(rdb, target, action)
        for target in (folder, sibling)
        for action in ("join", "request", "fetch", "add_source")
    )


def test_a_grant_is_reused_across_runs_and_never_asked_for_twice(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    session = _start(rdb, conn)
    flats = _flats(rdb, session)
    _approve(rdb, conn, session, _item(flats, "join"))
    _approve(rdb, conn, session, _item(None, "global_search"), cfg=SEARCH_CFG)

    for _run in range(3):  # authorized never consumes: every run reads the same grant
        assert research.authorized(rdb, flats, "join")
        assert research.search_granted(rdb, session.id, "global_search")
    with pytest.raises(research.ResearchError, match="already approved"):
        research.approval_summary(rdb, conn, CFG, session.id, [_item(flats, "join")])
    text = research.approval_summary(
        rdb, conn, CFG, session.id, [_item(flats, "join", "fetch", "add_source")]
    )
    assert "join it as" not in text
    assert "(already approved, not asked again: join)" in text
    assert "fetch its history" in text
    mixed = research.approval_summary(
        rdb,
        conn,
        SEARCH_CFG,
        session.id,
        [_item(None, "global_search"), _item(flats, "fetch", "add_source")],
    )
    assert "Already approved and not asked again: session searches." in mixed


def test_a_stopped_session_has_no_live_grants(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    session = _start(rdb, conn)
    flats = _flats(rdb, session)
    _approve(rdb, conn, session, _item(flats, "join", "fetch", "add_source"))
    _approve(rdb, conn, session, _item(None, "global_search"), cfg=SEARCH_CFG)

    assert research.stop(rdb, CFG, session.id) == 2

    assert research_db.list_grants(rdb, session.id, live_only=True) == []
    assert not research.authorized(rdb, flats, "join")
    assert not research.search_granted(rdb, session.id, "global_search")
    with pytest.raises(research.SessionStopped):
        research.approval_summary(rdb, conn, CFG, session.id, [_item(flats, "fetch")])
    with pytest.raises(research.SessionStopped):
        research.grant(rdb, conn, CFG, session.id, [_item(flats, "fetch")], via="cli", summary="x")
    with pytest.raises(research.UnknownSession):
        research.stop(rdb, CFG, 999)


def test_a_grant_for_another_account_is_never_written(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    """A session acts as its one account, so a grant naming another could authorize nothing;
    the store refuses it rather than every check filtering it out."""
    session = _start(rdb, conn)
    flats = _flats(rdb, session)
    with pytest.raises(ValueError, match="acts as default"):
        research_db.add_grant(
            rdb,
            session_id=session.id,
            candidate_id=flats.id,
            account="work",
            actions=["join"],
            via="cli",
            summary="join as work",
        )

    assert not research.authorized(rdb, flats, "join")
    assert research_db.list_grants(rdb, session.id) == []


def test_skip_and_exclude_narrow_without_consent_and_void_grants(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    session = _start(rdb, conn)
    other = _start(rdb, conn)
    flats = _flats(rdb, session)
    elsewhere = _flats(rdb, other)
    gated = _probed(rdb, session, "+JoinMe", "invite", type="supergroup", request_needed=True)
    done = _probed(rdb, session, "@tb_done", type="channel", status="fetched")
    _approve(rdb, conn, session, _item(gated, "request"))
    _approve(rdb, conn, other, _item(elsewhere, "join"))

    assert research.skip(rdb, CFG, session.id, [gated.id]) == [gated.id]
    assert research_db.get_candidate(rdb, gated.id).status == "skipped"  # type: ignore[union-attr]
    assert not research.authorized(rdb, gated, "request")
    assert research_db.live_grants(rdb, session.id, gated.id) == []
    with pytest.raises(research.ResearchError, match="skipping it would undo nothing"):
        research.skip(rdb, CFG, session.id, [done.id])
    with pytest.raises(research.UnknownCandidate):
        research.skip(rdb, CFG, session.id, [elsewhere.id])
    # a skipped candidate can be approved again
    _approve(rdb, conn, session, _item(gated, "request"))
    assert research.authorized(rdb, gated, "request")

    moved = research.exclude(rdb, CFG, ["https://t.me/tb_flats"], reason="spam")
    assert moved == {"@tb_flats": 2}
    assert not research.authorized(rdb, elsewhere, "join")
    assert research_db.live_grants(rdb, other.id, elsewhere.id) == []
    assert research.unexclude(rdb, CFG, [str(flats.id)], session_id=session.id) == ["@tb_flats"]
    assert research_db.get_candidate(rdb, elsewhere.id).status == "proposed"  # type: ignore[union-attr]
    assert not research.authorized(rdb, elsewhere, "join"), "lifting an exclusion approves nothing"
    assert research.unexclude(rdb, CFG, ["@tb_flats"]) == []


def test_targets_are_named_by_candidate_id_link_or_peer(rdb: sqlite3.Connection) -> None:
    session = research_db.create_session(
        rdb, question="q", account="default", seeds=[ChatKey("", SEED)], limits=ResearchLimits()
    )
    found = research_db.add_candidate(rdb, session.id, "+JoinMe", "invite", 1)
    assert found is not None

    assert research.target_identities(
        rdb,
        [str(found.id), "t.me/tb_flats/12", str(ORIGIN), "https://t.me/addlist/Tbilisi1"],
        session.id,
    ) == ["+JoinMe", "@tb_flats", f"peer:{ORIGIN}", "addlist/Tbilisi1"]
    with pytest.raises(research.ResearchError, match="needs a session"):
        research.target_identities(rdb, [str(found.id)])
    with pytest.raises(research.ResearchError, match="names no chat"):
        research.target_identities(rdb, ["peer:5"])
    with pytest.raises(research.ResearchError, match="names no chat"):
        research.target_identities(rdb, ["https://example.com"])
    with pytest.raises(research.UnknownCandidate):
        research.target_identities(rdb, ["999"], session.id)
    # ids past 64 bits name nothing — refused, never bound into a query that overflows
    with pytest.raises(research.UnknownCandidate):
        research.target_identities(rdb, [str(2**63)], session.id)
    with pytest.raises(research.ResearchError, match="no candidate"):
        research.target_identities(rdb, ["9" * 5000], session.id)
    for huge in (
        "https://t.me/c/99999999999999999999/5",
        "tg://privatepost?channel=99999999999999999999&post=5",
        "-99999999999999999999",
        "peer:-99999999999999999999",
    ):
        with pytest.raises(research.ResearchError, match="names no chat"):
            research.target_identities(rdb, [huge])
    assert research_db.get_candidate(rdb, 2**64) is None
    assert research_db.get_session(rdb, 2**64) is None
    assert research_db.excluded_by(rdb, "peer:99999999999999999999") is None


def test_approval_refuses_while_research_is_disabled(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    session = _start(rdb, conn)
    flats = _flats(rdb, session)
    off = Config()
    calls: list[Callable[[], object]] = [
        lambda: research.approval_summary(rdb, conn, off, session.id, [_item(flats, "join")]),
        lambda: research.grant(
            rdb, conn, off, session.id, [_item(flats, "join")], via="cli", summary="x"
        ),
        lambda: research.skip(rdb, off, session.id, [flats.id]),
        lambda: research.exclude(rdb, off, ["@tb_flats"]),
        lambda: research.unexclude(rdb, off, ["@tb_flats"]),
        lambda: research.stop(rdb, off, session.id),
    ]
    for call in calls:
        with pytest.raises(research.ResearchDisabled):
            call()
    assert research_db.list_grants(rdb, session.id) == []


# --- the run ---------------------------------------------------------------------------------

DEEP = make_channel(3010, "Deep rentals", username="deep_chan")
OPEN = make_channel(3011, "Open door", megagroup=True)


def _run_world(flats_posts: int = 3, **kwargs: Any) -> FakeWorld:
    """The probing world, with histories: ``@tb_flats`` ends with a post hiding a link to
    ``@deep_chan``, the gated group and the invite-only group hold a message each."""
    flats = [
        tl.message(_marked(FLATS), i, f"flat {i} in Vake", date=tl.at(i))
        for i in range(1, flats_posts)
    ]
    flats.append(
        tl.hyperlink_message(
            _marked(FLATS),
            flats_posts,
            "more rentals here",
            anchor="here",
            url="https://t.me/deep_chan",
            date=tl.at(flats_posts),
        )
    )
    world = _world(
        messages={
            _marked(FLATS): flats,
            _marked(GATED): [tl.message(_marked(GATED), 1, "welcome", date=tl.at(1))],
            _marked(OPEN): [tl.message(_marked(OPEN), 1, "hello", date=tl.at(1))],
            _marked(DEEP): [tl.message(_marked(DEEP), 1, "deep", date=tl.at(1))],
        },
        **kwargs,
    )
    world.entities[_marked(DEEP)] = DEEP
    world.entities[_marked(OPEN)] = OPEN
    world.invites["OpenDoor"] = FakeInvite(OPEN)
    return world


def _run_client(world: FakeWorld, **kwargs: Any) -> FakeClient:
    responses = {functions.channels.GetFullChannelRequest: no_discussion}
    responses.update(kwargs.pop("responses", {}))
    return world.client("default", me=make_user(9, "Me"), responses=responses, **kwargs)


@pytest.fixture
def paths(tmp_path: Any) -> Paths:
    home = Paths.under(tmp_path / "home")
    home.ensure_dirs()
    config.save(CFG, home)
    return home


async def _discovered(
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    client: FakeClient,
    *targets: str,
    **limits: int,
) -> tuple[ResearchSession, dict[str, Candidate]]:
    """A session whose seed links to ``targets``, discovered and probed through ``client``."""
    _links(conn, *targets)
    session = _start(rdb, conn, (str(SEED),), **limits)
    await research.discover(rdb, conn, CFG, session.id, client, now=3)
    found = {c.identity: c for c in research_db.list_candidates(rdb, session.id)}
    return session, found


async def _run(
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    paths: Paths,
    client: FakeClient,
    session: ResearchSession,
    budget: sync.SyncBudget | None = None,
) -> RunReport:
    return await research.run(
        rdb, conn, CFG, paths, {"default": client}, session.id, budget, now=20
    )


def _status(rdb: sqlite3.Connection, candidate: Candidate) -> Candidate:
    current = research_db.get_candidate(rdb, candidate.id)
    assert current is not None
    return current


def _live(rdb: sqlite3.Connection, candidate: Candidate) -> list[Grant]:
    return research_db.live_grants(rdb, candidate.session_id, candidate.id)


def _stored(conn: sqlite3.Connection, peer: int) -> list[int]:
    return [
        int(row[0])
        for row in conn.execute(
            "SELECT msg_id FROM messages WHERE chat_id = ? ORDER BY msg_id", (peer,)
        )
    ]


async def test_a_run_joins_fetches_and_only_proposes_what_it_finds(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, paths: Paths
) -> None:
    client = _run_client(_run_world())
    session, found = await _discovered(rdb, conn, client, "@tb_flats")
    flats = found["@tb_flats"]
    _approve(rdb, conn, session, _item(flats, "join", "fetch", "add_source"))

    report = await _run(rdb, conn, paths, client, session)

    assert (report.joined, report.sources_added, report.fetched) == ([flats.id],) * 3
    assert report.messages == 3 and report.stopped_by is None
    assert _stored(conn, _marked(FLATS)) == [1, 2, 3]
    (source,) = config.load(paths).sources
    assert source == Source(
        chat=_marked(FLATS), since=research.horizon(session), comments=True, account="default"
    ), "a joined chat's source names the probed peer, not a username that could move"
    done = _status(rdb, flats)
    assert (done.status, done.member, done.source_id) == ("fetched", True, source.id)
    assert _live(rdb, flats) == [], "every approved action was carried out: the grant is used"
    # the hidden link in the fetched chat is followed one hop further — and only proposed
    assert report.discovery is not None
    (deep_id,) = report.discovery.new_candidates
    deep = research_db.get_candidate(rdb, deep_id)
    assert deep is not None and (deep.identity, deep.depth, deep.status) == (
        "@deep_chan",
        2,
        "proposed",
    )
    assert _live(rdb, deep) == [] and not research.authorized(rdb, deep, "fetch")
    assert _marked(DEEP) not in {c["chat_id"] for _, c in client.calls if "chat_id" in c}
    progress = research_db.get_session(rdb, session.id)
    assert progress is not None and progress.progress["runs"] == 1
    assert progress.progress["last_run"]["fetched"] == [flats.id]  # type: ignore[index]


async def test_a_pending_admission_is_asked_about_again_and_fetched_once_admitted(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, paths: Paths
) -> None:
    client = _run_client(_run_world())
    session, found = await _discovered(rdb, conn, client, "https://t.me/+JoinMe")
    gated = found["+JoinMe"]
    _approve(rdb, conn, session, _item(gated, "request", "fetch", "add_source"))

    first = await _run(rdb, conn, paths, client, session)
    assert first.pending_admission == [gated.id] and first.fetched == []
    waiting = _status(rdb, gated)
    assert (waiting.status, waiting.member) == ("pending_admission", False)
    assert _marked(GATED) in client.requested
    assert config.load(paths).sources == [], "no source for a chat the account cannot read"
    assert _live(rdb, gated), "the fetch waits for the admins; nobody is asked again"

    still = await _run(rdb, conn, paths, client, session)
    assert still.admitted == [] and _status(rdb, gated).status == "pending_admission"
    assert _status(rdb, gated).note == research.PENDING_NOTE

    client.join(GATED)  # the chat's admins admit the account
    second = await _run(rdb, conn, paths, client, session)
    assert second.admitted == [gated.id]
    assert second.sources_added == second.fetched == [gated.id]
    (source,) = config.load(paths).sources
    assert source.chat == _marked(GATED) and not source.comments
    assert _stored(conn, _marked(GATED)) == [1]
    assert _status(rdb, gated).status == "fetched" and _live(rdb, gated) == []


async def test_a_message_cap_stops_the_run_and_the_next_one_resumes(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, paths: Paths
) -> None:
    client = _run_client(_run_world(flats_posts=5))
    session, found = await _discovered(rdb, conn, client, "@tb_flats", max_messages_per_run=2)
    flats = found["@tb_flats"]
    _approve(rdb, conn, session, _item(flats, "fetch", "add_source"))

    first = await _run(rdb, conn, paths, client, session)
    assert first.partial == [flats.id] and first.messages == 2
    assert first.stopped_by == "messages" and first.joined == []
    assert _stored(conn, _marked(FLATS)) == [1, 2]
    chat = db.get_chat(conn, _marked(FLATS))
    assert chat is not None and chat.last_msg_id == 2 and chat.last_sync_at is None
    assert _status(rdb, flats).note == research.PARTIAL_NOTE and _live(rdb, flats)
    assert first.discovery is not None and first.discovery.new_candidates == []

    second = await _run(rdb, conn, paths, client, session)
    assert second.partial == [flats.id] and second.sources_added == []
    third = await _run(rdb, conn, paths, client, session)
    assert third.fetched == [flats.id] and third.stopped_by is None
    assert _stored(conn, _marked(FLATS)) == [1, 2, 3, 4, 5]
    assert len(config.load(paths).sources) == 1, "the source is added once"
    assert third.discovery is not None and len(third.discovery.new_candidates) == 1
    progress = research_db.get_session(rdb, session.id)
    assert progress is not None and progress.progress["messages"] == 5


async def test_a_spent_clock_leaves_every_granted_step_for_the_next_run(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, paths: Paths
) -> None:
    client = _run_client(_run_world())
    session, found = await _discovered(rdb, conn, client, "@tb_flats")
    flats = found["@tb_flats"]
    _approve(rdb, conn, session, _item(flats, "join", "fetch", "add_source"))

    report = await _run(rdb, conn, paths, client, session, sync.SyncBudget(0))

    assert report.stopped_by == "time" and report.joined == report.fetched == []
    assert _status(rdb, flats).status == "approved" and _live(rdb, flats)
    assert not any(isinstance(r, functions.channels.JoinChannelRequest) for r in client.requests)
    assert config.load(paths).sources == [] and _history_calls(client) == []


async def test_sources_a_run_added_survive_stop_and_a_stopped_session_refuses_to_run(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, paths: Paths
) -> None:
    client = _run_client(_run_world())
    session, found = await _discovered(rdb, conn, client, "@tb_flats")
    _approve(rdb, conn, session, _item(found["@tb_flats"], "fetch", "add_source"))
    await _run(rdb, conn, paths, client, session)

    research.stop(rdb, CFG, session.id)

    assert [s.id for s in config.load(paths).sources] == ["chat:@tb_flats"]
    assert _stored(conn, _marked(FLATS)) == [1, 2, 3]
    with pytest.raises(research.SessionStopped):
        await _run(rdb, conn, paths, client, session)
    with pytest.raises(research.ResearchDisabled):
        await research.run(rdb, conn, Config(), paths, {"default": client}, session.id)


async def test_a_run_needs_the_session_s_own_account(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, paths: Paths
) -> None:
    session = _start(rdb, conn)
    with pytest.raises(research.ResearchError, match="not signed in") as refused:
        await research.run(rdb, conn, CFG, paths, {"work": _run_client(_run_world())}, session.id)
    assert refused.value.hint == tg.auth_hint("default")


async def test_only_the_approved_chats_of_a_shared_folder_are_joined(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, paths: Paths
) -> None:
    client = _run_client(_run_world())
    session, found = await _discovered(rdb, conn, client, "https://t.me/addlist/Tbilisi1")
    folder = found["addlist/Tbilisi1"]
    children = {c.identity: c for c in research_db.list_candidates(rdb, session.id)}
    private = children[f"peer:{_marked(FOLDER_PRIVATE)}"]
    sibling = children["@folder_chan"]
    assert private.parent_id == sibling.parent_id == folder.id
    _approve(rdb, conn, session, _item(private, "join"))

    report = await _run(rdb, conn, paths, client, session)

    assert report.joined == [private.id]
    assert client.chatlist_joins == [[_marked(FOLDER_PRIVATE)]]
    assert "Tbilisi1" in client.chatlists_joined
    assert _status(rdb, private).status == "joined" and _live(rdb, private) == []
    assert _status(rdb, sibling).status == "proposed" and _marked(FOLDER_CHAN) not in (
        client.members
    ), "a chat found in the same folder is never acted on without its own approval"
    assert config.load(paths).sources == [], "a join-only approval adds no source"
    assert report.discovery is None


async def test_an_imported_folder_takes_its_missing_chat_through_an_update(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, paths: Paths
) -> None:
    """A folder the account imported already gets the approved chat through
    ``chatlists.joinChatlistUpdates`` naming the filter the folder became, which Telegram
    accepts only for a folder the account holds and a chat that folder lists."""
    client = _run_client(_run_world(), members=[FOLDER_CHAN], chatlists_joined={"Tbilisi1"})
    session, found = await _discovered(rdb, conn, client, "https://t.me/addlist/Tbilisi1")
    private = found[f"peer:{_marked(FOLDER_PRIVATE)}"]
    _approve(rdb, conn, session, _item(private, "join"))

    report = await _run(rdb, conn, paths, client, session)

    assert report.joined == [private.id]
    assert client.chatlist_joins == [[_marked(FOLDER_PRIVATE)]]
    assert _marked(FOLDER_PRIVATE) in client.members


async def test_the_fake_joins_only_a_folder_s_own_chats_of_a_folder_it_holds() -> None:
    """The fake refuses what Telegram refuses, so a run naming another chat, or updating a
    folder the account never imported, cannot pass for a join."""
    client = _run_client(_run_world())
    outside = utils.get_input_peer(client.entities[_marked(GATED)])  # not in the folder
    inside = utils.get_input_peer(client.entities[_marked(FOLDER_PRIVATE)])
    joined = functions.chatlists.JoinChatlistInviteRequest(slug="Tbilisi1", peers=[outside])
    with pytest.raises(errors.BadRequestError, match="PEER_ID_INVALID"):
        await client(joined)
    update = functions.chatlists.JoinChatlistUpdatesRequest(
        chatlist=types.InputChatlistDialogFilter(filter_id=FakeClient.filter_id("Tbilisi1")),
        peers=[inside],
    )
    with pytest.raises(errors.BadRequestError, match="FILTER_ID_INVALID"):
        await client(update)
    assert client.chatlist_joins == []
    await client(functions.chatlists.JoinChatlistInviteRequest(slug="Tbilisi1", peers=[inside]))
    assert client.chatlist_joins == [[_marked(FOLDER_PRIVATE)]]


async def test_refusals_are_recorded_for_what_they_are(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, paths: Paths
) -> None:
    world = _run_world()
    client = _run_client(
        world, responses={functions.channels.JoinChannelRequest: errors.ChannelsTooMuchError(None)}
    )
    session, found = await _discovered(
        rdb, conn, client, "@tb_flats", "https://t.me/+OpenDoor", "https://t.me/+PeekIn"
    )
    flats, door, peek = found["@tb_flats"], found["+OpenDoor"], found["+PeekIn"]
    _approve(
        rdb,
        conn,
        session,
        _item(flats, "join"),
        _item(door, "join", "fetch", "add_source"),
        _item(peek, "join"),
    )
    client.join(OPEN)  # the account got in some other way since the approval
    world.invites["PeekIn"] = errors.InviteHashExpiredError(request=None)

    report = await _run(rdb, conn, paths, client, session)

    too_many = _status(rdb, flats)
    assert report.failed == [flats.id] and too_many.status == "failed"
    assert too_many.note is not None and "as many channels and groups" in too_many.note
    assert _live(rdb, flats) == [], "a failed step waits for a fresh approval"
    expired = _status(rdb, peek)
    assert report.unavailable == [peek.id] and expired.status == "unavailable"
    assert expired.note is not None and "refused the join" in expired.note
    member = _status(rdb, door)
    assert door.id in report.joined and member.peer_id == _marked(OPEN)
    assert report.fetched == [door.id] and _stored(conn, _marked(OPEN)) == [1]


async def test_a_flood_wait_on_a_join_stops_the_run_and_keeps_the_grants(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, paths: Paths
) -> None:
    flood = errors.FloodWaitError(request=None, capture=30)
    client = _run_client(_run_world(), responses={functions.channels.JoinChannelRequest: flood})
    session, found = await _discovered(rdb, conn, client, "@tb_flats")
    flats = found["@tb_flats"]
    _approve(rdb, conn, session, _item(flats, "join", "fetch", "add_source"))

    report = await _run(rdb, conn, paths, client, session)

    assert report.stopped_by == "flood" and "30s" in report.warnings[0]
    assert _status(rdb, flats).status == "approved" and _live(rdb, flats)
    assert config.load(paths).sources == []


async def test_a_source_removed_since_is_never_added_back(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, paths: Paths
) -> None:
    client = _run_client(_run_world(flats_posts=5))
    session, found = await _discovered(rdb, conn, client, "@tb_flats", max_messages_per_run=2)
    flats = found["@tb_flats"]
    _approve(rdb, conn, session, _item(flats, "fetch", "add_source"))
    await _run(rdb, conn, paths, client, session)
    config.update(paths, lambda cfg: dataclasses.replace(cfg, sources=[]))

    report = await _run(rdb, conn, paths, client, session)

    assert config.load(paths).sources == [] and report.partial == report.fetched == []
    assert "was removed from the config" in report.warnings[0]
    assert _live(rdb, flats) == [] and _stored(conn, _marked(FLATS)) == [1, 2]


async def test_an_imported_chat_is_never_taken_over_by_a_run(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, paths: Paths
) -> None:
    client = _run_client(_run_world())
    session, found = await _discovered(rdb, conn, client, "@tb_flats")
    flats = found["@tb_flats"]
    db.upsert_chat(
        conn,
        ChatRow(id=_marked(FLATS), type="channel", title="Flats", source_id="import:flats"),
    )
    _approve(rdb, conn, session, _item(flats, "fetch", "add_source"))

    report = await _run(rdb, conn, paths, client, session)

    assert report.failed == [flats.id] and config.load(paths).sources == []
    note = _status(rdb, flats).note
    assert note is not None and "import:flats" in note
    stored = db.get_chat(conn, _marked(FLATS))
    assert stored is not None and stored.source_id == "import:flats"


async def test_a_run_with_nothing_granted_touches_nothing(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, paths: Paths
) -> None:
    client = _run_client(_run_world())
    session, _ = await _discovered(rdb, conn, client, "@tb_flats", "https://t.me/+OpenDoor")
    sent = len(client.requests)

    report = await _run(rdb, conn, paths, client, session)

    assert report == RunReport(session_id=session.id)
    assert len(client.requests) == sent and _history_calls(client) == []
    assert config.load(paths).sources == []


# --- what the CLI and the MCP server answer with ---------------------------------------------


def test_approval_items_read_back_what_they_print() -> None:
    items = research.parse_approval(
        ["12:join,fetch,add_source", "7", "global_search", "paid_search", " 3:fetch, add_source"]
    )

    assert items == [
        ApprovalItem(candidate_id=12, actions=("join", "fetch", "add_source")),
        ApprovalItem(candidate_id=7, actions=()),
        ApprovalItem(candidate_id=None, actions=("global_search",)),
        ApprovalItem(candidate_id=None, actions=("paid_search",)),
        ApprovalItem(candidate_id=3, actions=("fetch", "add_source")),
    ]
    assert research.parse_approval(research.approval_args(items)) == items
    assert research.approve_command(4, items[:2]) == (
        "grepogram research approve 4 12:join,fetch,add_source 7"
    )


@pytest.mark.parametrize(
    "tokens",
    # "²" and "٣" pass str.isdigit and make int() raise: a digit is an ASCII one
    [
        [],
        ["abc"],
        ["12:"],
        ["-3:fetch"],
        ["global_search:x"],
        ["²"],
        ["٣:fetch"],
        ["1²:join"],
        ["99999999999999999999:fetch"],
        pytest.param(["9" * 5000], id="5000-digits"),
    ],
)
def test_a_malformed_approval_item_is_refused(tokens: list[str]) -> None:
    with pytest.raises(research.ResearchError) as caught:
        research.parse_approval(tokens)
    assert caught.value.hint == research.APPROVAL_GRAMMAR


def test_a_bare_id_approves_what_indexing_the_chat_takes(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    session = _start(rdb, conn)
    public = _flats(rdb, session)
    open_invite = _probed(rdb, session, "+OpenDoor", "invite", type="supergroup", member=False)
    gated = _probed(
        rdb, session, "+JoinMe", "invite", type="supergroup", member=False, request_needed=True
    )
    inside = _probed(rdb, session, "+Already", "invite", type="supergroup", member=True)
    named = [str(c.id) for c in (public, open_invite, gated, inside)]

    filled = research.with_default_actions(rdb, session.id, research.parse_approval(named))

    assert [item.actions for item in filled] == [
        ("join", "fetch", "add_source"),
        ("join", "fetch", "add_source"),
        ("request", "fetch", "add_source"),
        ("fetch", "add_source"),
    ]
    research.approval_summary(rdb, conn, CFG, session.id, filled)  # every one is grantable
    with pytest.raises(research.UnknownCandidate):
        research.with_default_actions(rdb, session.id, research.parse_approval(["999"]))


def test_a_candidate_document_keeps_member_cached_and_authorized_apart(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    session = _start(rdb, conn)
    _store(conn, SEED, 1, "rentals at @cachedchan", links=(("mention", "@cachedchan"),))
    research.discover_offline(rdb, conn, CFG, session.id, now=2)
    cached = research_db.candidate_by_identity(rdb, session.id, "@cachedchan")
    assert cached is not None
    research_db.update_candidate(
        rdb, cached.id, probed_at=3, type="channel", member=False, access_hash=77
    )
    _approve(rdb, conn, session, _item(cached, "fetch", "add_source"))

    document = research.candidates_document(rdb, conn, CFG, session.id)

    (entry,) = document["candidates"]
    assert (document["session_id"], document["account"], document["state"]) == (
        session.id,
        "default",
        "active",
    )
    assert (entry["identity"], entry["member"], entry["cached"]) == ("@cachedchan", False, True)
    assert (entry["cached_chats"], entry["cached_accounts"]) == ([CACHED], ["work"])
    assert entry["authorized"] == ["fetch", "add_source"]
    assert entry["evidence"][0]["via"] == "mention" and entry["evidence"][0]["msg_id"] == 1
    assert "access_hash" not in entry, "the account's access hash never leaves the store"
    assert research.candidates_document(rdb, conn, CFG, session.id, ["proposed"]) == {
        **document,
        "candidates": [],
    }
    with pytest.raises(research.ResearchError, match="unknown candidate status"):
        research.candidates_document(rdb, conn, CFG, session.id, ["maybe"])
    with pytest.raises(research.ResearchDisabled):
        research.candidates_document(rdb, conn, Config(), session.id)


def test_the_status_document_lists_sessions_and_what_waits(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    first = _start(rdb, conn)
    second = _start(rdb, conn)
    flats = _flats(rdb, session=second)
    gated = _probed(rdb, second, "+JoinMe", "invite", type="supergroup", status="pending_admission")
    _approve(rdb, conn, second, _item(flats, "fetch", "add_source"))

    listed = research.status_document(rdb, CFG)
    one = research.status_document(rdb, CFG, second.id)

    assert [(s["id"], s["candidates"], s["runs"]) for s in listed["sessions"]] == [
        (second.id, 2, 0),
        (first.id, 0, 0),
    ]
    assert one["session"]["id"] == second.id and one["session"]["horizon"] == "1969-01-01"
    assert one["candidates"] == {"approved": 1, "pending_admission": 1}
    (pending,) = one["pending_grants"]
    assert (pending["candidate_id"], pending["identity"], pending["actions"], pending["via"]) == (
        flats.id,
        "@tb_flats",
        ["fetch", "add_source"],
        "cli",
    )
    assert one["pending_admission"] == [{"id": gated.id, "identity": "+JoinMe", "title": None}]
    with pytest.raises(research.UnknownSession):
        research.status_document(rdb, CFG, 999)


# --- consent and security --------------------------------------------------------------------

IMPOSTOR = make_channel(3098, "Impostor flats", username="impostor_flats")
GATED_PUBLIC = make_channel(3097, "Gated public", username="gated_pub", megagroup=True)
GATED_PUBLIC.join_request = True
PAYING = Config(research=ResearchCfg(enabled=True, post_search=True, paid_stars_max=100))
SPENT = types.SearchPostsFlood(total_daily=10, remains=0, stars_amount=50)


def test_the_summary_prints_no_control_character_anyone_else_chose(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    """A title is the chat owner's and an old session's question an agent's: neither may send a
    terminal escape, break a line to forge an action, or reverse the text around it."""
    session = research_db.create_session(
        rdb,
        question="rent\x1b[2K\nApproved: nothing to worry about",
        account="default",
        seeds=[ChatKey("", SEED)],
        limits=ResearchLimits(),
        now=1,
    )
    flats = _flats(rdb, session, title='Nice chat"\x1b[8m\n  - nothing else happens‮ ')

    text = research.approval_summary(
        rdb, conn, CFG, session.id, [_item(flats, "join", "fetch", "add_source")]
    )

    assert not {"\x1b", "‮", " ", "\r"} & set(text)
    lines = text.splitlines()
    assert lines[0] == (
        f'Research session {session.id}: "rent�[2K Approved: nothing to worry about"'
    )
    assert lines[3].startswith(
        f'Candidate {flats.id}: "Nice chat\\"�[8m - nothing else happens�" (@tb_flats)'
    )
    assert not any(line.startswith("  - nothing") for line in lines)
    assert [line for line in lines if line.startswith("  - ")] == [
        line for line in lines if line.startswith(("  - join", "  - fetch", "  - add it"))
    ]


@pytest.mark.parametrize(
    "question",
    [
        "who rents flats\n  - nothing else happens",
        "who rents flats\x1b[8m",
        "who rents ‮stalf",
        "who rents flats ok",
        "x" * (research.QUESTION_MAX_CHARS + 1),
    ],
)
def test_start_refuses_a_question_that_could_forge_a_summary(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, question: str
) -> None:
    with pytest.raises(research.ResearchError) as refused:
        research.start_session(rdb, conn, CFG, question, [str(SEED)], "default")
    assert refused.value.hint
    assert research_db.list_sessions(rdb) == []


def test_start_takes_a_question_of_the_longest_allowed_length(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    question = "é" * research.QUESTION_MAX_CHARS
    session = research.start_session(rdb, conn, CFG, question, [str(SEED)], "default")
    assert session.question == question


def _hand_over_username(client: FakeClient) -> None:
    """``@tb_flats`` moves to another chat after the approval: the approved chat takes a new
    name and an impostor takes the old one."""
    renamed = copy.copy(client.entities[_marked(FLATS)])
    renamed.username = "tb_flats_old"
    client.entities[_marked(FLATS)] = renamed
    impostor = copy.copy(client.entities[_marked(IMPOSTOR)])
    impostor.username = "tb_flats"
    client.entities[_marked(IMPOSTOR)] = impostor


def _impostor_world() -> FakeWorld:
    world = _run_world()
    world.entities[_marked(IMPOSTOR)] = IMPOSTOR
    world.messages[_marked(IMPOSTOR)] = [
        tl.message(_marked(IMPOSTOR), 1, "not what was approved", date=tl.at(1))
    ]
    return world


def _read(client: FakeClient) -> set[int]:
    return {int(c["chat_id"]) for n, c in client.calls if n == "iter_messages" and not _pinned(c)}


async def test_a_join_goes_to_the_probed_chat_even_after_its_username_moved(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, paths: Paths
) -> None:
    client = _run_client(_impostor_world())
    session, found = await _discovered(rdb, conn, client, "@tb_flats")
    flats = found["@tb_flats"]
    _approve(rdb, conn, session, _item(flats, "join", "fetch", "add_source"))
    _hand_over_username(client)

    report = await _run(rdb, conn, paths, client, session)

    assert report.joined == report.fetched == [flats.id]
    assert _marked(FLATS) in client.members and _marked(IMPOSTOR) not in client.members
    (join,) = [r for r in client.requests if isinstance(r, functions.channels.JoinChannelRequest)]
    assert join.channel.channel_id == FLATS.id, "joined by the probed peer, not the username"
    (source,) = config.load(paths).sources
    assert source.chat == _marked(FLATS)
    assert _marked(IMPOSTOR) not in _read(client) and _stored(conn, _marked(IMPOSTOR)) == []
    assert _status(rdb, flats).peer_id == _marked(FLATS)


async def test_a_username_that_now_names_another_chat_is_never_joined(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, paths: Paths
) -> None:
    """With no access hash to address the probed peer, the join resolves the username — and
    refuses when it no longer names that peer."""
    client = _run_client(_impostor_world())
    session, found = await _discovered(rdb, conn, client, "@tb_flats")
    flats = research_db.update_candidate(rdb, found["@tb_flats"].id, access_hash=None)
    _approve(rdb, conn, session, _item(flats, "join", "fetch", "add_source"))
    _hand_over_username(client)

    report = await _run(rdb, conn, paths, client, session)

    assert report.unavailable == [flats.id] and report.joined == []
    stored = _status(rdb, flats)
    assert stored.status == "unavailable" and stored.peer_id == _marked(FLATS)
    assert "now names a different chat" in (stored.note or "")
    assert not [r for r in client.requests if isinstance(r, functions.channels.JoinChannelRequest)]
    assert config.load(paths).sources == [] and _live(rdb, flats) == []
    assert _read(client) == set()


async def test_a_public_chat_read_without_joining_is_checked_before_it_is_added(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, paths: Paths
) -> None:
    client = _run_client(_impostor_world())
    session, found = await _discovered(rdb, conn, client, "@tb_flats")
    flats = found["@tb_flats"]
    _approve(rdb, conn, session, _item(flats, "fetch", "add_source"))
    _hand_over_username(client)

    report = await _run(rdb, conn, paths, client, session)

    assert report.unavailable == [flats.id] and report.sources_added == []
    assert "now names a different chat" in (_status(rdb, flats).note or "")
    assert config.load(paths).sources == [] and _read(client) == set()


async def test_an_unmoved_public_chat_read_without_joining_is_added_by_its_username(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, paths: Paths
) -> None:
    client = _run_client(_impostor_world())
    session, found = await _discovered(rdb, conn, client, "@tb_flats")
    flats = found["@tb_flats"]
    _approve(rdb, conn, session, _item(flats, "fetch", "add_source"))

    report = await _run(rdb, conn, paths, client, session)

    assert report.fetched == [flats.id] and report.joined == []
    (source,) = config.load(paths).sources
    assert source.chat == "@tb_flats" and _marked(FLATS) not in client.members


def _moved_folder(client: FakeClient, world: FakeWorld) -> None:
    """A shared folder listing the chat that took ``@tb_flats`` over."""
    world.chatlists["Moved"] = FakeChatlist("Moved housing", [client.entities[_marked(IMPOSTOR)]])


def _set_aside(rdb: sqlite3.Connection, flats: Candidate, status: str) -> None:
    """``flats`` still names the chat it was probed as, and nothing is approved for it."""
    stored = _status(rdb, flats)
    assert stored.peer_id == _marked(FLATS)
    assert stored.access_hash == FakeWorld.access_hash("default", _marked(FLATS))
    assert stored.title == "Tbilisi flats"
    assert stored.status == status and "now leads to a different chat" in (stored.note or "")
    assert _live(rdb, flats) == []


async def test_a_folder_listing_the_chat_a_username_moved_to_never_repoints_the_approval(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, paths: Paths
) -> None:
    world = _impostor_world()
    client = _run_client(world)
    session, found = await _discovered(rdb, conn, client, "@tb_flats")
    flats = found["@tb_flats"]
    _approve(rdb, conn, session, _item(flats, "join", "fetch", "add_source"))
    _hand_over_username(client)
    _moved_folder(client, world)
    _store(conn, SEED, 2, "see t.me/addlist/Moved", links=(("link", "addlist/Moved"),))

    await research.discover(rdb, conn, CFG, session.id, client, now=6)

    _set_aside(rdb, flats, "failed")
    other = research_db.candidate_by_identity(rdb, session.id, f"peer:{_marked(IMPOSTOR)}")
    assert other is not None and other.id != flats.id
    assert (other.status, other.username, other.peer_id) == (
        "proposed",
        "tb_flats",
        _marked(IMPOSTOR),
    )
    assert _live(rdb, other) == []

    report = await _run(rdb, conn, paths, client, session)

    assert report.joined == report.fetched == report.sources_added == []
    assert not [r for r in client.requests if isinstance(r, functions.channels.JoinChannelRequest)]
    assert client.members.isdisjoint({_marked(FLATS), _marked(IMPOSTOR)})
    assert config.load(paths).sources == [] and _read(client) == set()


async def test_a_search_result_under_a_moved_username_never_repoints_the_approval(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, paths: Paths
) -> None:
    client = _run_client(_impostor_world())
    _links(conn, "@tb_flats")
    session = _asking(rdb, conn, "impostor")
    await research.discover(rdb, conn, SEARCH_CFG, session.id, client, now=3)
    flats = research_db.candidate_by_identity(rdb, session.id, "@tb_flats")
    assert flats is not None and flats.peer_id == _marked(FLATS)
    _approve(rdb, conn, session, _item(flats, "fetch", "add_source"))
    _hand_over_username(client)
    _grant(rdb, session, "global_search")

    (report,) = await research.global_search(
        client, rdb, conn, SEARCH_CFG, session.id, "impostor", kinds=["chat_search"], now=6
    )

    _set_aside(rdb, flats, "failed")
    other = research_db.candidate_by_identity(rdb, session.id, f"peer:{_marked(IMPOSTOR)}")
    assert other is not None and report.new_candidates == [other.id]
    run = await _run(rdb, conn, paths, client, session)
    assert run.fetched == run.sources_added == [] and config.load(paths).sources == []
    assert _read(client) == set()


async def test_an_undecided_candidate_whose_name_moved_is_unavailable_not_repointed(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    world = _impostor_world()
    client = _run_client(world)
    session, found = await _discovered(rdb, conn, client, "@tb_flats")
    _hand_over_username(client)
    _moved_folder(client, world)
    _store(conn, SEED, 2, "see t.me/addlist/Moved", links=(("link", "addlist/Moved"),))

    await research.discover(rdb, conn, CFG, session.id, client, now=6)

    flats = found["@tb_flats"]
    _set_aside(rdb, flats, "unavailable")
    views = research.candidate_views(rdb, conn, session)
    assert sorted(v.candidate.peer_id or 0 for v in views if v.candidate.kind != "addlist") == (
        sorted([_marked(FLATS), _marked(IMPOSTOR)])
    ), "two chats stay two candidates, never merged through the shared name"


async def test_an_admission_recheck_never_takes_the_chat_a_username_moved_to(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, paths: Paths
) -> None:
    world = _impostor_world()
    gated = copy.copy(world.entities[_marked(FLATS)])
    gated.join_request = True
    world.entities[_marked(FLATS)] = gated
    client = _run_client(world)
    session, found = await _discovered(rdb, conn, client, "@tb_flats")
    flats = found["@tb_flats"]
    assert flats.request_needed is True
    _approve(rdb, conn, session, _item(flats, "request", "fetch", "add_source"))
    first = await _run(rdb, conn, paths, client, session)
    assert first.pending_admission == [flats.id]
    _hand_over_username(client)
    client.members.add(_marked(IMPOSTOR))  # the account happens to be in the other chat

    second = await _run(rdb, conn, paths, client, session)

    assert second.admitted == [] and second.failed == [flats.id]
    _set_aside(rdb, flats, "failed")
    assert second.fetched == second.sources_added == [] and config.load(paths).sources == []
    assert _read(client) == set()


def test_a_candidate_s_peer_id_is_never_rewritten(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    session = _start(rdb, conn)
    first = research_db.add_candidate(rdb, session.id, "@name", "username", 1, peer_id=-1001)
    assert first is not None
    with pytest.raises(ValueError, match="cannot become"):
        research_db.update_candidate(rdb, first.id, peer_id=-1002)
    assert research_db.update_candidate(rdb, first.id, peer_id=-1001).peer_id == -1001
    other = research_db.add_candidate(
        rdb, session.id, "@name", "username", 1, peer_id=-1002, username="name"
    )
    assert other is not None and other.identity == "peer:-1002" and other.kind == "peer"
    assert research_db.candidate_for(rdb, session.id, "@name", peer_id=-1002) == other
    assert research_db.same_chat_candidates(rdb, other) == []


async def test_an_invite_that_now_leads_elsewhere_is_not_taken_for_the_approved_chat(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, paths: Paths
) -> None:
    world = _run_world()
    client = _run_client(world)
    session, found = await _discovered(rdb, conn, client, "https://t.me/+PeekIn")
    peek = found["+PeekIn"]
    assert peek.peer_id == _marked(PEEK)
    (bare,) = research.with_default_actions(
        rdb, session.id, [ApprovalItem(candidate_id=peek.id, actions=())]
    )
    assert bare.actions == ("join", "fetch", "add_source")
    _approve(rdb, conn, session, bare)
    world.invites["PeekIn"] = FakeInvite(OPEN)  # the link was handed to another group since

    report = await _run(rdb, conn, paths, client, session)

    assert report.failed == [peek.id] and report.joined == []
    stored = _status(rdb, peek)
    assert stored.status == "failed" and stored.peer_id == _marked(PEEK), "never overwritten"
    assert f'different chat "Open door" (id {_marked(OPEN)})' in (stored.note or "")
    assert f"grepogram leave --account default -- {_marked(OPEN)}" in (stored.note or "")
    assert config.load(paths).sources == [] and _live(rdb, peek) == []
    assert _read(client) == set()


async def test_an_exclusion_withdraws_what_is_still_approved_for_a_joined_chat(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, paths: Paths
) -> None:
    client = _run_client(_run_world())
    session, found = await _discovered(rdb, conn, client, "@tb_flats")
    flats = found["@tb_flats"]
    _approve(rdb, conn, session, _item(flats, "join", "fetch", "add_source"))
    # a run joined, then stopped (a flood wait, the clock) before adding and fetching
    client.join(FLATS)
    research_db.update_candidate(rdb, flats.id, status="joined", member=True)
    assert research.authorized(rdb, flats, "fetch")

    excluded = research.exclude(rdb, CFG, [str(_marked(FLATS))])  # its id, not its @name

    assert excluded == {f"peer:{_marked(FLATS)}": 1}
    assert _live(rdb, flats) == [] and not research.authorized(rdb, flats, "fetch")
    assert _status(rdb, flats).status == "joined", "what happened on Telegram stays recorded"
    report = await _run(rdb, conn, paths, client, session)
    assert report.sources_added == report.fetched == []
    assert config.load(paths).sources == [] and _stored(conn, _marked(FLATS)) == []


async def test_skipping_a_joined_chat_withdraws_its_pending_fetch(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, paths: Paths
) -> None:
    client = _run_client(_run_world())
    session, found = await _discovered(rdb, conn, client, "@tb_flats")
    flats = found["@tb_flats"]
    _approve(rdb, conn, session, _item(flats, "join", "fetch", "add_source"))
    client.join(FLATS)
    research_db.update_candidate(rdb, flats.id, status="joined", member=True)

    assert research.skip(rdb, CFG, session.id, [flats.id]) == [flats.id]

    assert _live(rdb, flats) == [] and _status(rdb, flats).member is True
    report = await _run(rdb, conn, paths, client, session)
    assert report.sources_added == report.fetched == [] and config.load(paths).sources == []
    # set aside, it can still be approved again: the account is in, so only the fetch is asked
    (again,) = research.with_default_actions(
        rdb, session.id, [ApprovalItem(candidate_id=flats.id, actions=())]
    )
    assert again.actions == ("fetch", "add_source")


def test_an_exclusion_under_any_spelling_denies_authorization(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    session = _start(rdb, conn)
    flats = _flats(rdb, session, peer_id=_marked(FLATS))
    _approve(rdb, conn, session, _item(flats, "join", "fetch", "add_source"))
    # written behind research_db's back, so no grant was voided: authorized asks itself
    rdb.execute(
        "INSERT INTO exclusions(identity, created_at) VALUES (?, 1)", (f"peer:{_marked(FLATS)}",)
    )

    assert _live(rdb, flats) and not research.authorized(rdb, flats, "fetch")


async def test_one_chat_reached_by_three_spellings_is_one_candidate(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    world = _world()
    world.invites["FlatsLink"] = FakeInvite(FLATS, peek=True)
    client = world.client("default")
    _links(conn, "@tb_flats")
    session = _start(rdb, conn)
    await research.discover(rdb, conn, CFG, session.id, client, now=3)
    (flats,) = research_db.list_candidates(rdb, session.id)
    assert flats.peer_id == _marked(FLATS)

    # the same chat by its id (a private post link) and by an invite link
    _store(conn, SEED, 10, "see", links=(("link", "https://t.me/c/3001/5"),))
    _store(conn, SEED, 11, "join", links=(("link", "https://t.me/+FlatsLink"),))
    report = await research.discover(rdb, conn, CFG, session.id, client, now=4)

    (merged,) = research_db.list_candidates(rdb, session.id)
    assert merged.id == flats.id and merged.identity == "@tb_flats"
    assert merged.invite_hash == "FlatsLink" and merged.peer_id == _marked(FLATS)
    assert research_db.corroboration(rdb, [merged.id]) == {merged.id: 3}
    assert report.updated_candidates == [flats.id]
    assert report.probe is not None and report.probe.probed == [flats.id]

    again = await research.discover(rdb, conn, CFG, session.id, client, now=5)
    assert again.new_candidates == [] and len(research_db.list_candidates(rdb, session.id)) == 1


def test_an_exclusion_covers_every_spelling_of_the_chat(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    session = _start(rdb, conn)
    flats = _flats(rdb, session, peer_id=_marked(FLATS))
    research_db.update_candidate(rdb, flats.id, invite_hash="FlatsLink")
    other = _start(rdb, conn)

    assert research.exclude(rdb, CFG, ["https://t.me/+FlatsLink"]) == {"+FlatsLink": 1}

    assert _status(rdb, flats).status == "excluded"
    spellings: list[tuple[str, Any, dict[str, Any]]] = [
        ("@tb_flats", "username", {"username": "tb_flats"}),
        (f"peer:{_marked(FLATS)}", "peer", {"peer_id": _marked(FLATS)}),
        ("+FlatsLink", "invite", {"invite_hash": "FlatsLink"}),
    ]
    for identity, kind, known in spellings:
        assert research_db.add_candidate(rdb, other.id, identity, kind, 1, **known) is None
    # a peer candidate a probe ties to the excluded chat later is excluded then
    later = research_db.add_candidate(rdb, other.id, "peer:-1000000009999", "peer", 1)
    assert later is not None
    tied = research._settle(rdb, later, "probed", 3, {"username": "tb_flats"})
    assert tied.status == "excluded"

    assert research.unexclude(rdb, CFG, ["+FlatsLink"]) == ["+FlatsLink"]
    assert _status(rdb, flats).status == "proposed"


async def test_a_public_chat_whose_admins_approve_joins_is_approved_as_a_request(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, paths: Paths
) -> None:
    world = _run_world()
    world.entities[_marked(GATED_PUBLIC)] = GATED_PUBLIC
    client = _run_client(world)
    session, found = await _discovered(rdb, conn, client, "@gated_pub")
    gated = found["@gated_pub"]
    assert gated.request_needed is True, "the channel's join_request flag is read"

    (bare,) = research.with_default_actions(
        rdb, session.id, [ApprovalItem(candidate_id=gated.id, actions=())]
    )
    assert bare.actions == ("request", "fetch", "add_source")
    with pytest.raises(research.ResearchError, match="approve `request` instead of `join`"):
        research.approval_summary(rdb, conn, CFG, session.id, [_item(gated, "join")])
    text = research.approval_summary(rdb, conn, CFG, session.id, [bare])
    assert "its admins approve who joins" in text and "send a request to join it" in text
    _approve(rdb, conn, session, bare)

    report = await _run(rdb, conn, paths, client, session)

    assert report.pending_admission == [gated.id] and report.joined == []
    assert _marked(GATED_PUBLIC) in client.requested


def test_the_summary_says_when_the_chat_is_already_covered_by_a_source(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    session = _start(rdb, conn)
    work_source = Source(chat="@cachedchan", since="2024-01-01", account="work")
    db.upsert_chat(
        conn,
        ChatRow(
            id=CACHED,
            type="channel",
            title="Already here",
            username="CachedChan",
            source_id=work_source.id,
        ),
    )
    cached = _probed(rdb, session, "@cachedchan", title="Already here", type="channel")
    cached = research_db.update_candidate(rdb, cached.id, peer_id=CACHED, member=False)
    items = [_item(cached, "fetch", "add_source")]

    elsewhere = Config(research=ResearchCfg(enabled=True), sources=[work_source])
    text = research.approval_summary(rdb, conn, elsewhere, session.id, items)
    assert (
        "  - fetch its history into the local index through work/chat:@cachedchan, the source "
        "that already covers it: as account work, since 2024-01-01, without the comments of "
        "its discussion group"
    ) in text
    assert "  - add it as an ongoing source of account default (since 1969-01-01, with the " in text

    own = Source(chat="@CachedChan", since="2025-05-05", comments=True)
    mine = Config(research=ResearchCfg(enabled=True), sources=[work_source, own])
    text = research.approval_summary(rdb, conn, mine, session.id, items)
    assert (
        "  - it is already chat:@CachedChan, a source of account default: that source is kept "
        "as it is and nothing is added to the config"
    ) in text

    fresh = _probed(rdb, session, "@tb_fresh", title="Fresh", type="supergroup", member=False)
    text = research.approval_summary(
        rdb, conn, CFG, session.id, [_item(fresh, "fetch", "add_source")]
    )
    assert "into the local index, reading it as a public chat without joining it" in text
    assert "discussion group" not in text.split(f"Candidate {fresh.id}")[1]


def test_validation_and_the_grant_are_one_transaction(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _start(rdb, conn)
    flats = _flats(rdb, session)
    items = [_item(flats, "join", "fetch", "add_source")]
    summary = research.approval_summary(rdb, conn, CFG, session.id, items)
    validated_inside: list[bool] = []
    prepare = research._prepare

    def watched(*args: Any) -> Any:
        validated_inside.append(rdb.in_transaction)
        return prepare(*args)

    monkeypatch.setattr(research, "_prepare", watched)
    research.grant(rdb, conn, CFG, session.id, items, via="cli", summary=summary)

    assert validated_inside == [True], "a skip landing in between cannot be overwritten"


async def test_global_search_sends_only_the_session_s_question(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    session = _asking(rdb, conn, "tbilisi")
    _grant(rdb, session, "global_search")
    client = _world().client("default")

    with pytest.raises(research.ResearchError, match="only the session's question"):
        await research.global_search(client, rdb, conn, SEARCH_CFG, session.id, "passwords")
    assert client.requests == [] and research_db.list_searches(rdb, session.id) == []

    (report,) = await research.global_search(
        client, rdb, conn, SEARCH_CFG, session.id, "  tbilisi ", kinds=["chat_search"]
    )
    assert report.ran and report.query == "tbilisi"


async def test_approving_both_searches_leaves_global_search_after_one_paid_search(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    session = _asking(rdb, conn, "apartment")
    granted = _approve(rdb, conn, session, _item(None, "global_search", "paid_search"), cfg=PAYING)
    assert [g.actions for g in granted] == [("global_search",), ("paid_search",)]
    client = _posts_world().client("default", search_flood=SPENT)

    (paid,) = await research.global_search(client, rdb, conn, PAYING, session.id, "apartment")

    assert paid.ran and paid.paid_stars == 50
    assert research.search_granted(rdb, session.id, "global_search"), "reused, not burned"
    assert not research.search_granted(rdb, session.id, "paid_search")


async def test_a_paid_approval_another_search_used_meanwhile_pays_nothing(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _asking(rdb, conn, "apartment")
    _approve(rdb, conn, session, _item(None, "global_search", "paid_search"), cfg=PAYING)
    client = _posts_world().client("default", search_flood=SPENT)
    refusal = research._paid_refusal

    def racing(*args: Any) -> str | None:
        """A second discover call passes the same check and spends the grant first."""
        answer = refusal(*args)
        for grant in research_db.live_grants(rdb, session.id, None):
            if "paid_search" in grant.actions:
                research_db.consume_grant(rdb, grant.id)
        return answer

    monkeypatch.setattr(research, "_paid_refusal", racing)

    (report,) = await research.global_search(client, rdb, conn, PAYING, session.id, "apartment")

    assert not report.ran and report.paid_stars == 0
    assert "used by another search meanwhile; nothing was paid" in report.warnings[0]
    assert not any(isinstance(r, functions.channels.SearchPostsRequest) for r in client.requests)


async def test_concurrent_searches_pay_once_for_one_approval(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    session = _asking(rdb, conn, "apartment")
    _approve(rdb, conn, session, _item(None, "global_search", "paid_search"), cfg=PAYING)
    client = _posts_world().client("default", search_flood=SPENT)

    reports = await asyncio.gather(
        *(
            research.global_search(client, rdb, conn, PAYING, session.id, "apartment")
            for _ in range(3)
        )
    )

    paid = [r for r in client.requests if isinstance(r, functions.channels.SearchPostsRequest)]
    assert [r.allow_paid_stars for r in paid] == [50]
    assert sum(report.paid_stars for (report,) in reports) == 50


async def test_a_paid_search_telegram_refuses_says_its_approval_is_spent(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    session = _asking(rdb, conn, "apartment")
    _approve(rdb, conn, session, _item(None, "global_search", "paid_search"), cfg=PAYING)
    refused = errors.BadRequestError(None, "STARS_INSUFFICIENT", 400)
    client = _posts_world().client(
        "default", search_flood=SPENT, responses={functions.channels.SearchPostsRequest: refused}
    )

    (report,) = await research.global_search(client, rdb, conn, PAYING, session.id, "apartment")

    assert not report.ran
    assert "the paid search failed and its paid_search approval is spent" in report.warnings[0]
    assert "Telegram refused the search" in report.warnings[1]
    assert not research.search_granted(rdb, session.id, "paid_search")


# --- review phase 1c: discovery completeness -------------------------------------------------

HOP_CHAN = make_channel(500, "Hop", username="hop_chan")
HOP_ORIGIN_CHAN = make_channel(600, "Hop origin", username="hop_origin")
DEEP_ORIGIN_CHAN = make_channel(700, "Deep origin", username="deep_origin")
WORK_CFG = Config(accounts=[AccountCfg(name="work")], research=ResearchCfg(enabled=True))


def _channel_full(channel: Any, group: Any) -> Callable[[Any], Any]:
    """``channels.getFullChannel`` of ``channel`` linking ``group`` as its discussion group."""

    def answer(request: Any) -> Any:
        full = no_discussion(request)
        full.full_chat.linked_chat_id = group.id
        full.chats = [channel, group]
        return full

    return answer


async def test_a_forward_chain_is_followed_one_hop_per_fetched_chat_up_to_the_depth_cap(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, paths: Paths
) -> None:
    """Nothing is stored by hand: the seed is synced, each forward's origin is known only by what
    the sync was handed with it — its username and this account's access hash — and each hop is
    probed, approved and fetched by a real run before the next one is even proposed."""
    hop, hop_origin = _marked(HOP_CHAN), _marked(HOP_ORIGIN_CHAN)
    world = FakeWorld(
        entities=[SEED_CHANNEL, HOP_CHAN, HOP_ORIGIN_CHAN, DEEP_ORIGIN_CHAN],
        messages={
            SEED: [tl.channel_forward(SEED, 1, "from hop", channel=HOP_CHAN, post=1)],
            hop: [tl.channel_forward(hop, 1, "from further", channel=HOP_ORIGIN_CHAN, post=1)],
            hop_origin: [
                tl.channel_forward(hop_origin, 1, "from the deep", channel=DEEP_ORIGIN_CHAN, post=1)
            ],
        },
    )
    client = _run_client(world, members=[SEED_CHANNEL])
    config.save(dataclasses.replace(CFG, sources=[Source(chat="@TbRent")]), paths)
    await sync.sync_all(
        {"default": client}, conn, functools.partial(config.load, paths), paths, sync.SyncBudget()
    )
    assert db.get_chat(conn, hop) is None, "the origin is no chat of the index"
    assert db.cached_peer_username(conn, hop) == "hop_chan"
    session = _start(rdb, conn, max_depth=2)

    first = await research.discover(rdb, conn, CFG, session.id, client, now=3)

    (candidate_id,) = first.new_candidates
    found = research_db.get_candidate(rdb, candidate_id)
    assert found is not None and (found.identity, found.depth, found.username) == (
        f"peer:{hop}",
        1,
        "hop_chan",
    )
    assert first.probe is not None and first.probe.probed == [found.id], "not a dead end"
    found = _status(rdb, found)
    assert (found.title, found.member) == ("Hop", False)
    _approve(rdb, conn, session, _item(found, "join", "fetch", "add_source"))

    ran = await _run(rdb, conn, paths, client, session)

    assert ran.joined == ran.fetched == [found.id] and _stored(conn, hop) == [1]
    assert ran.discovery is not None
    (next_id,) = ran.discovery.new_candidates
    further = research_db.get_candidate(rdb, next_id)
    assert further is not None and (further.identity, further.depth, further.status) == (
        f"peer:{hop_origin}",
        2,
        "proposed",
    )
    await research.discover(rdb, conn, CFG, session.id, client, now=21)
    _approve(rdb, conn, session, _item(_status(rdb, further), "join", "fetch", "add_source"))

    last = await _run(rdb, conn, paths, client, session)

    assert last.fetched == [further.id] and last.discovery is not None
    assert last.discovery.new_candidates == [] and last.discovery.beyond_depth == 1
    assert f"peer:{_marked(DEEP_ORIGIN_CHAN)}" not in _by_identity(rdb, conn, session)


async def test_the_pinned_posts_of_a_seed_are_read_for_leads_and_stored_nowhere(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    """A directory keeps its index in an old pin, long before any ``since``: discover reads the
    seeds' pins once, keeps their leads as evidence and writes no message and no cursor."""
    ancient = tl.EPOCH - dt.timedelta(days=3000)
    pin = tl.mention_message(SEED, 1, "our list: @tb_flats @folder_chan", date=ancient, pinned=True)
    world = _world(messages={SEED: [pin, tl.mention_message(SEED, 2, "loose @banned_from")]})
    client = world.client("default", members=[SEED_CHANNEL])
    _store(conn, SEED, 2, "loose")
    session = _start(rdb, conn)
    before, chat = _counts(conn), db.get_chat(conn, SEED)

    report = await research.discover(rdb, conn, CFG, session.id, client, now=3)

    assert report.pins is not None
    assert (report.pins.chats, report.pins.messages, report.pins.remaining) == ([SEED], 1, 0)
    found = _by_identity(rdb, conn, session)
    assert {identity: [e.via for e in v.evidence] for identity, v in found.items()} == {
        "@tb_flats": ["pinned"],
        "@folder_chan": ["pinned"],
    }
    evidence = found["@tb_flats"].evidence[0]
    assert (evidence.chat, evidence.msg_id) == (ChatKey("", SEED), 1)
    assert _counts(conn) == before and db.get_chat(conn, SEED) == chat, "stored nowhere"
    assert _pin_reads(client) == [SEED]

    again = await research.discover(rdb, conn, CFG, session.id, client, now=4)
    assert again.pins is not None and again.pins.chats == [] and _pin_reads(client) == [SEED]
    with pytest.raises(research.ResearchDisabled):
        await research.read_pins(client, rdb, conn, Config(), session.id)


async def test_a_run_reads_the_pinned_posts_of_what_it_fetched_whatever_their_age(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, paths: Paths
) -> None:
    flats = _marked(FLATS)
    world = _world(
        messages={
            flats: [
                tl.mention_message(
                    flats,
                    1,
                    "index: @deep_chan",
                    date=tl.EPOCH - dt.timedelta(days=900),
                    pinned=True,
                ),
                tl.message(flats, 2, "flat in Vake", date=tl.at(2)),
            ],
            _marked(DEEP): [tl.message(_marked(DEEP), 1, "deep", date=tl.at(1))],
        }
    )
    world.entities[_marked(DEEP)] = DEEP
    client = _run_client(world)
    _links(conn, "@tb_flats")
    started = int(tl.EPOCH.timestamp())
    session = research.start_session(
        rdb, conn, CFG, QUESTION, [str(SEED)], "default", {"since_days": 1}, now=started
    )
    await research.discover(rdb, conn, CFG, session.id, client, now=started)
    candidate = _by_identity(rdb, conn, session)["@tb_flats"].candidate
    _approve(rdb, conn, session, _item(candidate, "fetch", "add_source"))

    report = await research.run(rdb, conn, CFG, paths, {"default": client}, session.id, now=started)

    assert report.fetched == [candidate.id]
    assert _stored(conn, flats) == [2], "the pin is older than the source's since"
    assert report.pins is not None and report.pins.chats == [flats]
    (deep_id,) = report.pins.new_candidates
    deep = research_db.get_candidate(rdb, deep_id)
    assert deep is not None and (deep.identity, deep.depth, deep.status) == (
        "@deep_chan",
        2,
        "proposed",
    )
    assert [e.via for e in research_db.list_evidence(rdb, deep_id)] == ["pinned"]
    assert not research.authorized(rdb, deep, "fetch")


def test_a_chat_that_lists_many_chats_is_a_directory_and_says_so_on_its_leads(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    names = [f"@listed_{n:02d}" for n in range(research.DIRECTORY_MIN_CHATS)]
    for msg_id, name in enumerate(names, start=1):
        _store(conn, SEED, msg_id, f"see {name}", links=(("mention", name),))
    _store(conn, SEED_TWO, 1, "just @one_chat", links=(("mention", "@one_chat"),))
    session = _start(rdb, conn, seeds=(str(SEED), str(SEED_TWO)))

    report = research.discover_offline(rdb, conn, CFG, session.id)

    assert report.directories == [SEED]
    found = _by_identity(rdb, conn, session)
    for name in names:
        view = found[name]
        assert sorted(e.via for e in view.evidence) == ["directory", "mention"]
        assert view.corroboration == 1, "the directory path is no second origin"
    assert [e.via for e in found["@one_chat"].evidence] == ["mention"]
    cursor = scan_cursor(rdb, session.id, ChatKey("", SEED))
    assert cursor is not None and cursor.directory


def test_comments_stored_out_of_order_and_a_seed_channel_s_group_are_read(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    """A channel seed's comments are part of what it says (depth 0), and a comment the group
    stores after later ones — its id below the newest — is read all the same."""
    group = -1000000000150
    db.upsert_chat(
        conn, ChatRow(id=group, type="supergroup", title="Rent chat", discussion_of=SEED)
    )
    db.set_discussion_chat(conn, SEED, group)
    _store(conn, group, 9, "late @late_chan", links=(("mention", "@late_chan"),))
    session = _start(rdb, conn)

    first = research.discover_offline(rdb, conn, CFG, session.id)

    assert first.chats_scanned == 2
    late = _by_identity(rdb, conn, session)["@late_chan"]
    assert late.candidate.depth == 1, "the group reads at its channel's depth"
    assert late.evidence[0].chat == ChatKey("", group)
    _store(conn, group, 4, "early @early_chan", links=(("mention", "@early_chan"),))

    second = research.discover_offline(rdb, conn, CFG, session.id)

    assert [research_db.get_candidate(rdb, c).identity for c in second.new_candidates] == [  # type: ignore[union-attr]
        "@early_chan"
    ]


def test_rows_whose_links_were_never_read_are_read_by_their_text_whatever_their_id(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    """An import stored after link capture began still has no links: its rows are read by their
    visible text, which the id threshold never allowed."""
    _store(conn, SEED, 1, "captured t.me/seen_chan", links=(("link", "@seen_chan"),))
    _store(conn, SEED, 2, "imported: t.me/import_chan", links=None)
    session = _start(rdb, conn)
    report = research.discover_offline(rdb, conn, CFG, session.id)
    assert set(_by_identity(rdb, conn, session)) == {"@seen_chan", "@import_chan"}
    assert report.text_fallback == 1


def test_the_session_ceiling_bounds_every_call_together(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    for msg_id, name in enumerate(("@one_chan", "@two_chan", "@three_chan"), start=1):
        _store(conn, SEED, msg_id, name, links=(("mention", name),))
    session = _start(rdb, conn, max_session_candidates=2)

    first = research.discover_offline(rdb, conn, CFG, session.id)

    assert len(first.new_candidates) == 2 and first.over_cap == 1
    assert first.session_full and not first.truncated
    assert scan_cursor(rdb, session.id, ChatKey("", SEED)) is not None, (
        "nothing held back could ever be proposed: the cursor moves on"
    )
    _store(conn, SEED, 4, "@four_chan", links=(("mention", "@four_chan"),))
    second = research.discover_offline(rdb, conn, CFG, session.id)
    assert second.new_candidates == [] and second.session_full
    assert research_db.count_candidates(rdb, session.id) == 2


async def test_discovery_reads_the_index_off_the_event_loop(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    offline = research.discover_offline

    def recorded(*args: Any, **kwargs: Any) -> Any:
        seen.append(threading.current_thread().name)
        return offline(*args, **kwargs)

    monkeypatch.setattr(research, "discover_offline", recorded)
    session = _start(rdb, conn)
    await research.discover(rdb, conn, CFG, session.id, _world().client("default"), now=3)
    assert seen and seen[0] != threading.main_thread().name


async def test_an_admission_request_nobody_answers_times_out(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, paths: Paths
) -> None:
    client = _run_client(_run_world())
    session, found = await _discovered(rdb, conn, client, "https://t.me/+JoinMe")
    gated = found["+JoinMe"]
    _approve(rdb, conn, session, _item(gated, "request", "fetch", "add_source"))
    await _run(rdb, conn, paths, client, session)
    assert _status(rdb, gated).requested_at == 20
    days = session.limits.admission_timeout_days

    within = await research.run(
        rdb, conn, CFG, paths, {"default": client}, session.id, now=20 + days * 86400 - 1
    )
    assert within.failed == [] and _status(rdb, gated).status == "pending_admission"
    late = await research.run(
        rdb, conn, CFG, paths, {"default": client}, session.id, now=20 + days * 86400
    )

    assert late.failed == [gated.id]
    given_up = _status(rdb, gated)
    assert given_up.status == "failed" and "no answer" in (given_up.note or "")
    assert _live(rdb, gated) == [], "a new approval may send the request again"


def test_evidence_names_its_chat_one_way_and_the_row_to_read_it_by(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    _store(conn, SEED, 3, "see @tb_flats", links=(("mention", "@tb_flats"),))
    session = _start(rdb, conn)
    research.discover_offline(rdb, conn, CFG, session.id)
    (candidate,) = research_db.list_candidates(rdb, session.id)
    research_db.add_evidence(
        rdb, candidate.id, "post_search", "post:-1000000009999/4", chat=ChatKey("", -1000000009999)
    )
    document = research.candidates_document(rdb, conn, CFG, session.id)
    indexed, searched = document["candidates"][0]["evidence"]
    assert (indexed["scope"], indexed["peer_id"], indexed["chat_id"], indexed["msg_id"]) == (
        "",
        SEED,
        SEED,
        3,
    )
    assert (searched["peer_id"], searched["chat_id"]) == (-1000000009999, None)
    assert "chat" not in indexed


async def test_a_busy_sync_leaves_the_approved_sources_for_the_next_run(
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    paths: Paths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _run_client(_run_world())
    session, found = await _discovered(rdb, conn, client, "@tb_flats")
    flats = found["@tb_flats"]
    _approve(rdb, conn, session, _item(flats, "join", "fetch", "add_source"))

    with sync.SyncLock(paths):
        held = await _run(rdb, conn, paths, client, session)

    assert held.stopped_by == "sync_busy" and held.joined == [flats.id]
    assert held.sources_added == [] and config.load(paths).sources == []
    assert _live(rdb, flats), "nothing is lost: the source and the fetch wait"
    real = sync.sync_all

    async def taken_meanwhile(*args: Any, **kwargs: Any) -> Any:
        with sync.SyncLock(paths):  # another process wins the lock after the config write
            return await real(*args, **kwargs)

    monkeypatch.setattr(sync, "sync_all", taken_meanwhile)
    between = await _run(rdb, conn, paths, client, session)

    assert between.stopped_by == "sync_busy" and between.sources_added == [flats.id]
    assert between.fetched == [] and _stored(conn, _marked(FLATS)) == []
    assert len(config.load(paths).sources) == 1 and _live(rdb, flats)
    monkeypatch.setattr(sync, "sync_all", real)

    resumed = await _run(rdb, conn, paths, client, session)

    assert resumed.stopped_by is None and resumed.fetched == [flats.id]
    assert resumed.sources_added == [] and len(config.load(paths).sources) == 1
    assert _stored(conn, _marked(FLATS)) == [1, 2, 3] and _live(rdb, flats) == []


async def test_a_run_reads_a_fetched_channel_s_comments_one_hop_further(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, paths: Paths
) -> None:
    flats = _marked(FLATS)
    group = make_channel(3050, "Flats chat", megagroup=True)
    world = _run_world()
    world.entities[_marked(group)] = group
    world.messages[flats] = [tl.channel_post(flats, 1, "a flat", replies=1, date=tl.at(1))]
    world.comments[(flats, 1)] = [
        tl.hyperlink_message(
            _marked(group), 5, "ask here", anchor="here", url="https://t.me/deep_chan", sender=1
        )
    ]
    client = _run_client(
        world, responses={functions.channels.GetFullChannelRequest: _channel_full(FLATS, group)}
    )
    session, found = await _discovered(rdb, conn, client, "@tb_flats")
    candidate = found["@tb_flats"]
    _approve(rdb, conn, session, _item(candidate, "join", "fetch", "add_source"))

    report = await _run(rdb, conn, paths, client, session)

    assert report.fetched == [candidate.id]
    assert _stored(conn, _marked(group)) == [5]
    cursor = scan_cursor(rdb, session.id, ChatKey("", _marked(group)))
    assert cursor is not None and cursor.depth == 1, "registered at the channel's depth"
    assert report.discovery is not None
    (deep_id,) = report.discovery.new_candidates
    deep = research_db.get_candidate(rdb, deep_id)
    assert deep is not None and (deep.identity, deep.depth) == ("@deep_chan", 2)
    (evidence,) = research_db.list_evidence(rdb, deep_id)
    assert evidence.chat == ChatKey("", _marked(group))


async def test_a_run_as_another_account_joins_and_adds_sources_as_that_account(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, paths: Paths
) -> None:
    config.save(WORK_CFG, paths)
    world = _run_world()
    home = _run_client(world)
    work = world.client(
        "work",
        me=make_user(8, "Work"),
        responses={functions.channels.GetFullChannelRequest: no_discussion},
    )
    _links(conn, "@tb_flats")
    session = research.start_session(rdb, conn, WORK_CFG, QUESTION, [str(SEED)], "work", now=1)
    await research.discover(rdb, conn, WORK_CFG, session.id, work, now=3)
    flats = _by_identity(rdb, conn, session)["@tb_flats"].candidate
    assert flats.access_hash == FakeWorld.access_hash("work", _marked(FLATS))
    _approve(rdb, conn, session, _item(flats, "join", "fetch", "add_source"), cfg=WORK_CFG)

    report = await research.run(
        rdb, conn, WORK_CFG, paths, {"default": home, "work": work}, session.id, now=20
    )

    assert report.joined == report.fetched == [flats.id]
    (join,) = [r for r in work.requests if isinstance(r, functions.channels.JoinChannelRequest)]
    assert join.channel.access_hash == FakeWorld.access_hash("work", _marked(FLATS))
    assert home.requests == [] and _history_calls(home) == [], "the default account is not asked"
    (source,) = config.load(paths).sources
    assert (source.account, source.chat) == ("work", _marked(FLATS))
    assert db.chat_reach(conn, _marked(FLATS)) == ["work"]
    cursor = scan_cursor(rdb, session.id, ChatKey("", _marked(FLATS)))
    assert cursor is not None and cursor.depth == 1


async def test_a_seed_is_the_same_conversation_after_its_rows_are_stored_again(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    """Work's chat with Bob sits under a synthetic id; the session names it ``(work, Bob)``, so
    re-adding both accounts' rows the other way round — work's now under Bob's own id — or
    rebuilding the index leaves the session reading work's conversation and nobody else's."""
    cfg = dataclasses.replace(two_accounts.CFG, research=ResearchCfg(enabled=True))
    two = two_accounts.load(conn)
    assert two.work_bob.id >= db.SYNTHETIC_BASE
    _store(conn, two.work_bob.id, 20, "try @work_lead", links=(("mention", "@work_lead"),))
    _store(conn, two.default_bob.id, 20, "try @home_lead", links=(("mention", "@home_lead"),))
    session = research.start_session(
        rdb, conn, cfg, QUESTION, [f"work/{two_accounts.BOB}"], "work", now=1
    )
    assert session.seeds == (ChatKey("work", two_accounts.BOB),)
    research.discover_offline(rdb, conn, cfg, session.id)
    assert set(_by_identity(rdb, conn, session)) == {"@work_lead"}
    (row,) = research.candidates_document(rdb, conn, cfg, session.id)["candidates"][0]["evidence"]
    assert row["chat_id"] == two.work_bob.id

    for chat in (two.default_bob, two.work_bob):
        db.delete_chat(conn, chat.id)
    work_bob = db.upsert_chat(conn, dataclasses.replace(two.work_bob, id=two_accounts.BOB), "work")
    home_bob = db.upsert_chat(conn, two.default_bob, "default")
    assert work_bob.id == two_accounts.BOB and home_bob.id > two.work_bob.id, "never reused"
    _store(conn, work_bob.id, 21, "and @work_again", links=(("mention", "@work_again"),))
    _store(conn, home_bob.id, 21, "and @home_again", links=(("mention", "@home_again"),))

    research.discover_offline(rdb, conn, cfg, session.id)

    assert set(_by_identity(rdb, conn, session)) == {"@work_lead", "@work_again"}
    again = research_db.list_evidence(
        rdb, _by_identity(rdb, conn, session)["@work_again"].candidate.id
    )
    assert again[0].chat == ChatKey("work", two_accounts.BOB)

    rebuilt = db.connect(":memory:")
    try:
        db.migrate(rebuilt)
        fresh = db.upsert_chat(rebuilt, dataclasses.replace(two.work_bob, id=2), "work")
        _store(rebuilt, fresh.id, 1, "fresh @work_fresh", links=(("mention", "@work_fresh"),))
        research.discover_offline(rdb, rebuilt, cfg, session.id)
        assert "@work_fresh" in _by_identity(rdb, rebuilt, session), "another index: read afresh"
    finally:
        rebuilt.close()


async def test_another_account_s_private_seed_is_never_asked_about(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    cfg = dataclasses.replace(two_accounts.CFG, research=ResearchCfg(enabled=True))
    two_accounts.load(conn)
    session = research.start_session(
        rdb, conn, cfg, QUESTION, [f"work/{two_accounts.BOB}"], "default", now=1
    )
    client = _world().client("default")

    pins = await research.read_pins(client, rdb, conn, cfg, session.id, now=2)

    assert pins.chats == [] and _pin_reads(client) == [] and client.calls == []
    assert "account work's own" in pins.warnings[0]
    assert (await research.read_pins(client, rdb, conn, cfg, session.id, now=3)).warnings == []


async def test_a_forward_origin_known_by_its_username_alone_is_probed_by_it(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    """A ``min`` origin brings no usable access hash, only its username: the probe resolves the
    name and takes the answer only when it is that very peer."""
    flats, secret = _marked(FLATS), _marked(SECRET)
    db.remember_peers(conn, "default", [(flats, "tb_flats", None), (secret, "tb_flats", None)])
    _store(conn, SEED, 1, "repost", fwd_peer_id=flats, fwd_msg_id=3)
    _store(conn, SEED, 2, "another", fwd_peer_id=secret, fwd_msg_id=4)
    session = _start(rdb, conn)
    research.discover_offline(rdb, conn, CFG, session.id)
    client = _world().client("default")

    report = await research.probe_candidates(client, rdb, conn, CFG, session.id)

    found = _by_identity(rdb, conn, session)
    named = found[f"peer:{flats}"].candidate
    assert named.id in report.probed and (named.username, named.title) == (
        "tb_flats",
        "Tbilisi flats",
    )
    stale = found[f"peer:{secret}"].candidate
    assert stale.id in report.unresolvable and "no longer names it" in (stale.note or "")


def test_a_stored_session_with_a_huge_horizon_still_answers(rdb: sqlite3.Connection) -> None:
    """A session a build without the ``since_days`` ceiling stored may ask for more days than
    the calendar holds; its horizon is the first date there is rather than an OverflowError on
    every later call."""
    session = research_db.create_session(
        rdb,
        question="q",
        account="default",
        seeds=[ChatKey("", SEED)],
        limits=ResearchLimits(since_days=10**7),
        now=0,
    )
    assert research.horizon(session) == "0001-01-01"
    assert research.session_document(session)["horizon"] == "0001-01-01"


def test_prepare_approval_is_what_both_consent_channels_show(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    """The CLI and the MCP tool both put this to the human: the defaults a bare id stands for,
    the summary :func:`research.grant` checks against, and the command that asks on a terminal."""
    session = _start(rdb, conn)
    flats = _flats(rdb, session)

    approval = research.prepare_approval(rdb, conn, CFG, session.id, [str(flats.id)])

    assert approval.items == (_item(flats, "join", "fetch", "add_source"),)
    assert approval.summary == research.approval_summary(rdb, conn, CFG, session.id, approval.items)
    assert (
        approval.command
        == f"grepogram research approve {session.id} {flats.id}:join,fetch,add_source"
    )
    with pytest.raises(research.ResearchError, match="not an approval item"):
        research.prepare_approval(rdb, conn, CFG, session.id, ["nine"])


@pytest.mark.parametrize("ref", ["²", "-²", "٣"])
def test_a_target_in_non_ascii_digits_names_no_chat(rdb: sqlite3.Connection, ref: str) -> None:
    with pytest.raises(research.ResearchError, match="names no chat"):
        research.target_identities(rdb, [ref], session_id=1)


async def test_a_refusal_quoting_a_folder_link_stays_out_of_the_log_above_debug(
    rdb: sqlite3.Connection,
    conn: sqlite3.Connection,
    paths: Paths,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A shared folder's link came out of someone's message and opens a private way in: the
    note naming it is the candidate's, and the log says only what became of the candidate."""
    world = _run_world()
    client = _run_client(world)
    session, found = await _discovered(rdb, conn, client, "https://t.me/addlist/Tbilisi1")
    private = {c.identity: c for c in research_db.list_candidates(rdb, session.id)}[
        f"peer:{_marked(FOLDER_PRIVATE)}"
    ]
    _approve(rdb, conn, session, _item(private, "join"))
    world.chatlists["Tbilisi1"] = FakeChatlist("Tbilisi housing", [FOLDER_CHAN])

    with caplog.at_level(logging.INFO, logger="grepogram"):
        report = await _run(rdb, conn, paths, client, session)

    assert report.unavailable == [private.id]
    assert "t.me/addlist/Tbilisi1" in (_status(rdb, private).note or "")
    assert f"research candidate {private.id} is unavailable" in caplog.text
    assert "Tbilisi1" not in caplog.text
