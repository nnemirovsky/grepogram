import dataclasses
import logging
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

from grepogram import db, research, research_db
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
