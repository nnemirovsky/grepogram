import dataclasses
import logging
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest
from telethon import errors, utils
from telethon.tl import functions, types

from grepogram import db, research, research_db, sync, tg
from grepogram.filters import UnknownChat
from grepogram.models import (
    CandidateView,
    ChatRow,
    Config,
    LinkKind,
    MessageRow,
    ResearchCfg,
    ResearchLimits,
    ResearchSession,
)
from tests.fakes import (
    FakeChatlist,
    FakeClient,
    FakeInvite,
    FakeWorld,
    make_channel,
    make_group,
    make_user,
)
from tests.fixtures import tl

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
    return research.start_session(
        rdb, conn, CFG, QUESTION, list(seeds), "default", ResearchLimits(**limits), now=1
    )


def _by_identity(
    rdb: sqlite3.Connection, conn: sqlite3.Connection, session: ResearchSession
) -> dict[str, CandidateView]:
    return {v.candidate.identity: v for v in research.candidate_views(rdb, conn, session)}


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
    assert session.seeds == tuple(sorted((SEED, SEED_TWO)))
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
    assert (evidence.chat_id, evidence.msg_id, evidence.snippet) == (SEED, 2, "look here")
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


def test_a_forward_chain_is_followed_one_hop_per_fetched_chat_up_to_the_depth_cap(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    _store(conn, SEED, 1, "from hop", fwd_peer_id=HOP, fwd_msg_id=1)
    session = _start(rdb, conn, max_depth=2)
    research.discover_offline(rdb, conn, CFG, session.id)
    assert [v.candidate.depth for v in research.candidate_views(rdb, conn, session)] == [1]

    # a run fetched the hop at the depth it was found at (task 16 records this cursor)
    db.upsert_chat(conn, ChatRow(id=HOP, type="channel", title="Hop"))
    _store(conn, HOP, 1, "from further", fwd_peer_id=HOP_ORIGIN, fwd_msg_id=1)
    research_db.set_scan_cursor(rdb, session.id, HOP, depth=1, msg_id=0)
    research.discover_offline(rdb, conn, CFG, session.id)
    found = _by_identity(rdb, conn, session)
    assert found[f"peer:{HOP_ORIGIN}"].candidate.depth == 2
    assert f"peer:{HOP}" in found, "the fetched chat stays a candidate"

    # the next hop would be depth 3: beyond max_depth, so never proposed
    db.upsert_chat(conn, ChatRow(id=HOP_ORIGIN, type="channel", title="Hop origin"))
    _store(conn, HOP_ORIGIN, 1, "from the deep", fwd_peer_id=DEEP_ORIGIN, fwd_msg_id=1)
    research_db.set_scan_cursor(rdb, session.id, HOP_ORIGIN, depth=2, msg_id=0)
    report = research.discover_offline(rdb, conn, CFG, session.id)
    assert f"peer:{DEEP_ORIGIN}" not in _by_identity(rdb, conn, session)
    assert report.beyond_depth == 1 and report.new_candidates == []


def test_a_deeper_path_adds_evidence_to_an_existing_candidate(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    _store(conn, SEED, 1, "t.me/alpha_rent", links=(("link", "@alpha_rent"),))
    session = _start(rdb, conn, max_depth=1)
    research.discover_offline(rdb, conn, CFG, session.id)
    db.upsert_chat(conn, ChatRow(id=HOP, type="channel", title="Hop"))
    _store(conn, HOP, 4, "t.me/alpha_rent again", links=(("link", "@alpha_rent"),))
    research_db.set_scan_cursor(rdb, session.id, HOP, depth=1, msg_id=0)

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
    cursor = research_db.scan_cursor(rdb, session.id, SEED)
    assert cursor is not None and (cursor.depth, cursor.msg_id) == (0, 1)

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
    assert research_db.scan_cursor(rdb, session.id, SEED) is None, "held back for the next call"

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


def _history_calls(client: FakeClient) -> list[str]:
    return [name for name, _ in client.calls if name in ("iter_messages", "get_messages")]


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
    _links(conn, "@tb_flats")
    session = _start(rdb, conn)
    research.discover_offline(rdb, conn, CFG, session.id)
    client = _world().client("default", members=[FLATS])

    await research.probe_candidates(client, rdb, conn, CFG, session.id)

    (candidate,) = research_db.list_candidates(rdb, session.id)
    assert candidate.member is True


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
    session = _start(rdb, conn)
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
    session = _start(rdb, conn)
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
    session = _start(rdb, conn)
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
    assert (evidence.chat_id, evidence.msg_id) == (_marked(FLATS), 11)
    assert evidence.origin_key == f"post:{_marked(FLATS)}/11"
    assert evidence.snippet == "Apartment in Vake for rent"
    assert _history_calls(client) == []


async def test_a_spent_quota_is_never_paid_for_by_default(
    rdb: sqlite3.Connection, conn: sqlite3.Connection
) -> None:
    session = _start(rdb, conn)
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
    session = _start(rdb, conn)
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
    session = _start(rdb, conn)
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
    session = _start(rdb, conn)
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
