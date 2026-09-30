import inspect
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from grepogram import db, research_db
from grepogram.models import ChatKey, Grant, ResearchLimits, ScanCursor
from grepogram.paths import Paths
from tests.conftest import file_mode, scan_cursor


@pytest.fixture
def paths(tmp_home: Path) -> Paths:
    return Paths.from_env()


@pytest.fixture
def rdb() -> Iterator[sqlite3.Connection]:
    connection = research_db.open_store(":memory:")
    yield connection
    connection.close()


def _session(rdb: sqlite3.Connection, question: str = "who sells apartments in Tbilisi") -> int:
    return research_db.create_session(
        rdb,
        question=question,
        account="default",
        seeds=[ChatKey("", -10), ChatKey("default", 20)],
        limits=ResearchLimits(),
        now=1,
    ).id


# --- file, mode and schema -------------------------------------------------------------------


def test_the_store_is_created_private_next_to_the_index(tmp_home: Path, paths: Paths) -> None:
    conn = research_db.open_store(paths)
    try:
        session = research_db.create_session(
            conn, question="q", account="default", seeds=[], limits=ResearchLimits()
        )
        journals = sorted(tmp_home.glob("research.db-*"))
        assert journals, "WAL mode keeps a -wal and a -shm file while the store is open"
        for journal in journals:
            assert file_mode(journal) == 0o600, journal
    finally:
        conn.close()
    assert paths.research_db_file == tmp_home / "research.db"
    assert file_mode(paths.research_db_file) == 0o600
    reopened = research_db.open_store(paths)
    try:
        assert research_db.schema_version(reopened) == research_db.SCHEMA_VERSION
        assert research_db.get_session(reopened, session.id) == session
    finally:
        reopened.close()


def test_a_permissive_existing_file_is_narrowed_to_0600(paths: Paths) -> None:
    paths.ensure_dirs()
    paths.research_db_file.touch(mode=0o644)
    paths.research_db_file.chmod(0o644)
    research_db.open_store(paths).close()
    assert file_mode(paths.research_db_file) == 0o600


def test_the_store_is_not_the_index(paths: Paths) -> None:
    research_db.open_store(paths).close()
    assert not paths.db_file.exists()


def test_a_newer_schema_is_refused_without_advice_to_delete(paths: Paths) -> None:
    conn = research_db.open_store(paths)
    conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    conn.close()
    with pytest.raises(research_db.SchemaError, match=r"v99 is newer") as caught:
        research_db.open_store(paths)
    assert isinstance(caught.value, db.SchemaError)
    assert "delete" not in str(caught.value) and "upgrade grepogram" in str(caught.value)


@pytest.mark.parametrize(
    ("setup", "match"),
    [
        ("CREATE TABLE sessions(id INTEGER)", "records no schema version"),
        ("CREATE TABLE meta(k TEXT, v TEXT)", "not grepogram's"),
        (
            "CREATE TABLE meta(key TEXT, value TEXT); "
            "INSERT INTO meta VALUES ('schema_version', 'x')",
            "'x' as its schema version",
        ),
        (
            "CREATE TABLE meta(key TEXT, value TEXT); "
            "INSERT INTO meta VALUES ('schema_version', '-1')",
            "cannot be upgraded",
        ),
    ],
)
def test_an_unknown_schema_is_refused(setup: str, match: str) -> None:
    conn = research_db.connect(":memory:")
    try:
        conn.executescript(setup)
        with pytest.raises(research_db.SchemaError, match=match):
            research_db.migrate(conn)
    finally:
        conn.close()


def test_migrations_run_from_one_without_a_gap() -> None:
    assert sorted(research_db.MIGRATIONS) == list(range(1, research_db.SCHEMA_VERSION + 1))


# --- sessions --------------------------------------------------------------------------------


def test_a_session_round_trips(rdb: sqlite3.Connection) -> None:
    limits = ResearchLimits(max_depth=3, max_candidates=5)
    created = research_db.create_session(
        rdb,
        question="  flats  ",
        account="work",
        seeds=[ChatKey("work", 3), ChatKey("", -1), ChatKey("work", 3)],
        limits=limits,
        now=100,
    )
    assert created.question == "flats" and created.account == "work"
    assert created.seeds == (ChatKey("work", 3), ChatKey("", -1)) and created.limits == limits
    assert created.state == "active" and created.stopped_at is None and created.progress == {}
    assert research_db.get_session(rdb, created.id) == created
    assert research_db.get_session(rdb, created.id + 1) is None


def test_a_session_needs_a_question(rdb: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        research_db.create_session(
            rdb, question="  ", account="default", seeds=[], limits=ResearchLimits()
        )
    assert research_db.list_sessions(rdb) == []


def test_progress_is_replaced(rdb: sqlite3.Connection) -> None:
    sid = _session(rdb)
    research_db.set_session_progress(rdb, sid, {"runs": 1, "fetched": [5]})
    research_db.set_session_progress(rdb, sid, {"runs": 2})
    session = research_db.get_session(rdb, sid)
    assert session is not None and session.progress == {"runs": 2}
    with pytest.raises(KeyError):
        research_db.set_session_progress(rdb, sid + 1, {})


def test_sessions_list_newest_first_and_by_state(rdb: sqlite3.Connection) -> None:
    first, second = _session(rdb, "a"), _session(rdb, "b")
    research_db.stop_session(rdb, first, now=5)
    assert [s.id for s in research_db.list_sessions(rdb)] == [second, first]
    assert [s.id for s in research_db.list_sessions(rdb, "active")] == [second]
    assert [s.id for s in research_db.list_sessions(rdb, "stopped")] == [first]


def test_stop_voids_unconsumed_grants_and_keeps_the_first_stop_time(
    rdb: sqlite3.Connection,
) -> None:
    sid = _session(rdb)
    cand = research_db.add_candidate(rdb, sid, "@a", "username", 1)
    assert cand is not None
    used = _grant(rdb, sid, cand.id, ["fetch", "add_source"])
    live = _grant(rdb, sid, cand.id, ["join"])
    search = _grant(rdb, sid, None, ["global_search"])
    assert research_db.consume_grant(rdb, used.id, now=2)
    assert research_db.stop_session(rdb, sid, now=10) == 2
    assert research_db.stop_session(rdb, sid, now=20) == 0
    session = research_db.get_session(rdb, sid)
    assert session is not None and session.state == "stopped" and session.stopped_at == 10
    grants = {g.id: g for g in research_db.list_grants(rdb, sid)}
    assert grants[used.id].consumed_at == 2 and grants[used.id].voided_at is None
    assert grants[live.id].voided_at == 10 and grants[search.id].voided_at == 10
    assert research_db.list_grants(rdb, sid, live_only=True) == []
    assert research_db.live_grants(rdb, sid, cand.id) == []
    candidate = research_db.get_candidate(rdb, cand.id)
    assert candidate is not None and candidate.status == "proposed"
    with pytest.raises(KeyError):
        research_db.stop_session(rdb, sid + 1)


# --- candidates ------------------------------------------------------------------------------


def test_a_candidate_is_unique_per_session_by_identity(rdb: sqlite3.Connection) -> None:
    sid = _session(rdb)
    first = research_db.add_candidate(rdb, sid, "@chan", "username", 2, now=1)
    again = research_db.add_candidate(
        rdb, sid, "@chan", "username", 1, username="chan", peer_id=-1001, now=9
    )
    assert first is not None and again is not None
    assert again.id == first.id
    assert again.depth == 1 and again.username == "chan" and again.peer_id == -1001
    assert again.created_at == 1 and again.status == "proposed"
    deeper = research_db.add_candidate(rdb, sid, "@chan", "username", 3, peer_id=-1002)
    assert deeper is not None and deeper.depth == 1 and deeper.peer_id == -1001
    assert [c.id for c in research_db.list_candidates(rdb, sid)] == [first.id]
    with pytest.raises(sqlite3.IntegrityError):
        rdb.execute(
            "INSERT INTO candidates(session_id, identity, kind, depth, created_at) "
            "VALUES (?, '@chan', 'username', 1, 0)",
            (sid,),
        )


def test_the_same_identity_is_a_candidate_of_each_session(rdb: sqlite3.Connection) -> None:
    one, two = _session(rdb, "a"), _session(rdb, "b")
    a = research_db.add_candidate(rdb, one, "+Hash", "invite", 1, invite_hash="Hash")
    b = research_db.add_candidate(rdb, two, "+Hash", "invite", 1, invite_hash="Hash")
    assert a is not None and b is not None and a.id != b.id
    assert research_db.candidate_by_identity(rdb, two, "+Hash") == b
    assert research_db.candidate_by_identity(rdb, two, "+other") is None


def test_candidate_updates_set_only_what_a_probe_or_run_learns(rdb: sqlite3.Connection) -> None:
    sid = _session(rdb)
    cand = research_db.add_candidate(rdb, sid, "@x", "username", 1)
    assert cand is not None
    updated = research_db.update_candidate(
        rdb,
        cand.id,
        title="X",
        type="channel",
        participants=1200,
        member=False,
        request_needed=True,
        access_hash=-5,
        probed_at=7,
        status="approved",
    )
    assert updated.title == "X" and updated.type == "channel" and updated.participants == 1200
    assert updated.member is False and updated.request_needed is True
    assert updated.access_hash == -5 and updated.probed_at == 7 and updated.status == "approved"
    assert research_db.get_candidate(rdb, cand.id) == updated
    assert research_db.update_candidate(rdb, cand.id) == updated
    with pytest.raises(ValueError, match="identity"):
        research_db.update_candidate(rdb, cand.id, identity="@y")
    with pytest.raises(KeyError):
        research_db.update_candidate(rdb, cand.id + 1, note="n")


def test_candidates_list_by_status_and_parent(rdb: sqlite3.Connection) -> None:
    sid = _session(rdb)
    folder = research_db.add_candidate(rdb, sid, "addlist/Slug", "addlist", 1, addlist_slug="Slug")
    assert folder is not None
    child = research_db.add_candidate(
        rdb, sid, "peer:-1009", "peer", 2, peer_id=-1009, parent_id=folder.id
    )
    other = research_db.add_candidate(rdb, sid, "@other", "username", 1)
    assert child is not None and other is not None
    research_db.update_candidate(rdb, other.id, status="skipped")
    assert [c.id for c in research_db.list_candidates(rdb, sid, parent_id=folder.id)] == [child.id]
    assert [c.id for c in research_db.list_candidates(rdb, sid, ["skipped"])] == [other.id]
    assert research_db.list_candidates(rdb, sid, []) == []
    with pytest.raises(ValueError):
        research_db.add_candidate(rdb, sid, "@z", "username", -1)


# --- evidence --------------------------------------------------------------------------------


def test_forwards_of_one_post_corroborate_once(rdb: sqlite3.Connection) -> None:
    sid = _session(rdb)
    cand = research_db.add_candidate(rdb, sid, "@origin", "username", 1)
    link = research_db.add_candidate(rdb, sid, "@linked", "username", 1)
    assert cand is not None and link is not None
    for chat in range(10):
        assert research_db.add_evidence(
            rdb, cand.id, "forward", "fwd:-1005/77", chat=ChatKey("", -chat), msg_id=1, snippet="s"
        )
    at = ChatKey("default", 1)
    assert research_db.add_evidence(rdb, cand.id, "link", "msg:default:1/2", chat=at, msg_id=2)
    assert not research_db.add_evidence(rdb, cand.id, "link", "msg:default:1/2", chat=at, msg_id=2)
    other = ChatKey("work", 1)
    assert research_db.add_evidence(rdb, cand.id, "link", "msg:default:1/2", chat=other, msg_id=2)
    assert research_db.list_evidence(rdb, cand.id)[-1].chat == other
    assert research_db.add_evidence(rdb, link.id, "post_search", "search:1:@linked")
    assert not research_db.add_evidence(rdb, link.id, "post_search", "search:1:@linked")
    assert len(research_db.list_evidence(rdb, cand.id)) == 12
    assert research_db.corroboration(rdb, [cand.id, link.id, 999]) == {cand.id: 2, link.id: 1}
    with pytest.raises(ValueError):
        research_db.add_evidence(rdb, cand.id, "link", "")


# --- grants ----------------------------------------------------------------------------------


def _grant(
    rdb: sqlite3.Connection, sid: int, candidate_id: int | None, actions: list[str]
) -> Grant:
    return research_db.add_grant(
        rdb,
        session_id=sid,
        candidate_id=candidate_id,
        account="default",
        actions=actions,  # type: ignore[arg-type]
        via="cli",
        summary="join @a as default",
        now=1,
    )


def test_a_grant_cannot_be_created_without_its_channel() -> None:
    parameter = inspect.signature(research_db.add_grant).parameters["via"]
    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    writers = [name for name in dir(research_db) if "grant" in name and name.startswith("add")]
    assert writers == ["add_grant"]


def test_grants_record_the_channel_and_stay_live_until_used(rdb: sqlite3.Connection) -> None:
    sid = _session(rdb)
    cand = research_db.add_candidate(rdb, sid, "@a", "username", 1)
    assert cand is not None
    grant = research_db.add_grant(
        rdb,
        session_id=sid,
        candidate_id=cand.id,
        account="default",
        actions=["join", "fetch", "join", "add_source"],
        via="elicitation",
        summary="join @a",
        now=3,
    )
    assert grant.via == "elicitation" and grant.actions == ("join", "fetch", "add_source")
    assert grant.live and grant.granted_at == 3 and grant.summary == "join @a"
    assert research_db.live_grants(rdb, sid, cand.id) == [grant]
    assert research_db.live_grants(rdb, sid, None) == []
    assert research_db.consume_grant(rdb, grant.id, now=4)
    assert not research_db.consume_grant(rdb, grant.id, now=5)
    assert research_db.live_grants(rdb, sid, cand.id) == []


@pytest.mark.parametrize("via", ["", "api", "tool", "yes"])
def test_a_grant_through_any_other_channel_is_refused(rdb: sqlite3.Connection, via: str) -> None:
    sid = _session(rdb)
    with pytest.raises(ValueError, match="grant channel"):
        research_db.add_grant(
            rdb,
            session_id=sid,
            candidate_id=None,
            account="default",
            actions=["global_search"],
            via=via,  # type: ignore[arg-type]
            summary="s",
        )
    assert research_db.list_grants(rdb, sid) == []


@pytest.mark.parametrize("via", ["NULL", "'api'"])
def test_the_schema_refuses_a_grant_without_a_known_channel(
    rdb: sqlite3.Connection, via: str
) -> None:
    sid = _session(rdb)
    with pytest.raises(sqlite3.IntegrityError):
        rdb.execute(
            "INSERT INTO grants(session_id, account, actions, via, summary, granted_at) "
            f"VALUES (?, 'default', '[\"global_search\"]', {via}, 's', 0)",
            (sid,),
        )


@pytest.mark.parametrize(
    ("on_candidate", "actions", "match"),
    [
        (True, [], "at least one action"),
        (True, ["global_search"], "candidate action"),
        (True, ["leave"], "candidate action"),
        (False, ["fetch"], "session action"),
    ],
)
def test_grant_actions_must_fit_their_target(
    rdb: sqlite3.Connection, on_candidate: bool, actions: list[str], match: str
) -> None:
    sid = _session(rdb)
    cand = research_db.add_candidate(rdb, sid, "@a", "username", 1)
    assert cand is not None
    with pytest.raises(ValueError, match=match):
        _grant(rdb, sid, cand.id if on_candidate else None, actions)


def test_grants_need_an_active_session_its_own_candidate_and_a_summary(
    rdb: sqlite3.Connection,
) -> None:
    one, two = _session(rdb, "a"), _session(rdb, "b")
    foreign = research_db.add_candidate(rdb, two, "@a", "username", 1)
    assert foreign is not None
    with pytest.raises(KeyError, match="no candidate"):
        _grant(rdb, one, foreign.id, ["fetch"])
    with pytest.raises(KeyError, match="no research session"):
        _grant(rdb, 999, None, ["global_search"])
    with pytest.raises(ValueError, match="approval text"):
        research_db.add_grant(
            rdb,
            session_id=two,
            candidate_id=foreign.id,
            account="default",
            actions=["fetch"],
            via="cli",
            summary=" ",
        )
    research_db.stop_session(rdb, two)
    with pytest.raises(ValueError, match="stopped"):
        _grant(rdb, two, foreign.id, ["fetch"])


def test_voiding_by_candidate_leaves_the_others_live(rdb: sqlite3.Connection) -> None:
    sid = _session(rdb)
    a = research_db.add_candidate(rdb, sid, "@a", "username", 1)
    b = research_db.add_candidate(rdb, sid, "@b", "username", 1)
    assert a is not None and b is not None
    _grant(rdb, sid, a.id, ["fetch"])
    kept = _grant(rdb, sid, b.id, ["fetch"])
    session_wide = _grant(rdb, sid, None, ["global_search"])
    assert research_db.void_grants(rdb, sid, candidate_ids=[a.id]) == 1
    assert research_db.list_grants(rdb, sid, live_only=True) == [kept, session_wide]
    assert research_db.live_grants(rdb, sid, None) == [session_wide]


# --- exclusions ------------------------------------------------------------------------------


def test_exclusions_are_global_and_persistent(paths: Paths) -> None:
    conn = research_db.open_store(paths)
    try:
        one = _session(conn, "a")
        research_db.add_exclusion(conn, "@spam", "ads", now=1)
        assert research_db.add_candidate(conn, one, "@spam", "username", 1) is None
    finally:
        conn.close()
    conn = research_db.open_store(paths)
    try:
        later = _session(conn, "b")
        assert research_db.is_excluded(conn, "@spam")
        assert research_db.add_candidate(conn, later, "@spam", "username", 1) is None
        assert research_db.list_candidates(conn, later) == []
        exclusions = research_db.list_exclusions(conn)
        assert [(e.identity, e.reason, e.created_at) for e in exclusions] == [("@spam", "ads", 1)]
    finally:
        conn.close()


def test_excluding_moves_pending_candidates_of_every_session_and_voids_their_grants(
    rdb: sqlite3.Connection,
) -> None:
    one, two = _session(rdb, "a"), _session(rdb, "b")
    pending = research_db.add_candidate(rdb, one, "@x", "username", 1)
    fetched = research_db.add_candidate(rdb, two, "@x", "username", 1)
    other = research_db.add_candidate(rdb, one, "@y", "username", 1)
    assert pending is not None and fetched is not None and other is not None
    research_db.update_candidate(rdb, fetched.id, status="fetched")
    research_db.update_candidate(rdb, pending.id, status="approved")
    _grant(rdb, one, pending.id, ["join"])
    kept = _grant(rdb, one, other.id, ["join"])
    assert research_db.add_exclusion(rdb, "@x", now=5) == 1
    assert research_db.add_exclusion(rdb, "@x", "again", now=6) == 0
    moved = research_db.get_candidate(rdb, pending.id)
    stayed = research_db.get_candidate(rdb, fetched.id)
    assert moved is not None and moved.status == "excluded"
    assert stayed is not None and stayed.status == "fetched"
    assert research_db.live_grants(rdb, one, pending.id) == []
    assert research_db.list_grants(rdb, one, live_only=True) == [kept]
    assert research_db.list_exclusions(rdb)[0].created_at == 5
    assert research_db.remove_exclusion(rdb, "@x")
    assert not research_db.remove_exclusion(rdb, "@x")
    restored = research_db.get_candidate(rdb, pending.id)
    assert restored is not None and restored.status == "proposed"
    assert research_db.live_grants(rdb, one, pending.id) == []
    assert research_db.add_candidate(rdb, one, "@x", "username", 1) == restored


# --- searches and scan cursors ---------------------------------------------------------------


def test_searches_are_recorded_per_session(rdb: sqlite3.Connection) -> None:
    sid = _session(rdb)
    record = research_db.record_search(
        rdb, sid, "post_search", "flats tbilisi", results=12, note="free quota 9", now=4
    )
    assert record.kind == "post_search" and record.results == 12 and record.ran_at == 4
    assert research_db.list_searches(rdb, sid) == [record]
    assert research_db.list_searches(rdb, sid + 1) == []


def test_a_scan_cursor_never_moves_back_nor_deepens(rdb: sqlite3.Connection) -> None:
    sid = _session(rdb)
    chat = ChatKey("", -10)
    assert scan_cursor(rdb, sid, chat) is None
    research_db.set_scan_cursor(rdb, sid, chat, depth=1, index_id="a", lead_seq=50, now=1)
    cursor = research_db.set_scan_cursor(rdb, sid, chat, depth=2, index_id="a", lead_seq=40, now=2)
    assert (cursor.depth, cursor.lead_seq, cursor.scanned_at) == (1, 50, 2)
    research_db.set_scan_cursor(rdb, sid, chat, depth=0, index_id="a", lead_seq=90, now=3)
    assert scan_cursor(rdb, sid, chat) == ScanCursor(
        session_id=sid, chat=chat, depth=0, index_id="a", lead_seq=90, scanned_at=3
    )
    research_db.set_scan_cursor(rdb, sid, ChatKey("default", 5), depth=1, index_id="a", now=3)
    assert [c.chat for c in research_db.list_scan_cursors(rdb, sid)] == [
        chat,
        ChatKey("default", 5),
    ]


def test_a_cursor_of_another_index_is_replaced_not_kept(rdb: sqlite3.Connection) -> None:
    """A rebuilt index restarts its lead clock: the old cursor would skip its rows."""
    sid = _session(rdb)
    chat = ChatKey("", -10)
    research_db.set_scan_cursor(rdb, sid, chat, depth=0, index_id="old", lead_seq=900, now=1)
    cursor = research_db.set_scan_cursor(rdb, sid, chat, depth=0, index_id="new", lead_seq=3, now=2)
    assert (cursor.index_id, cursor.lead_seq) == ("new", 3)


def test_pins_and_directories_are_remembered_beside_the_cursor(rdb: sqlite3.Connection) -> None:
    sid = _session(rdb)
    chat = ChatKey("", -10)
    research_db.set_scan_cursor(rdb, sid, chat, depth=1, index_id="a", lead_seq=7, now=1)
    research_db.mark_pins_read(rdb, sid, chat, depth=2, now=5)
    research_db.mark_directory(rdb, sid, chat, depth=2, now=6)
    cursor = scan_cursor(rdb, sid, chat)
    assert cursor is not None
    assert (cursor.depth, cursor.lead_seq, cursor.pins_read_at, cursor.directory) == (1, 7, 5, True)
    fresh = research_db.mark_pins_read(rdb, sid, ChatKey("work", 3), depth=0, now=8)
    assert (fresh.depth, fresh.lead_seq, fresh.index_id, fresh.directory) == (0, 0, None, False)


def test_step_two_names_v1_chats_by_scope_and_peer(rdb: sqlite3.Connection) -> None:
    """A v1 file named chats by index row id; step 2 turns them into ``(scope, peer_id)`` and
    drops the synthetic ids that named no peer."""
    v1 = research_db.connect(":memory:")
    try:
        for statement in research_db.MIGRATIONS[1]:
            v1.execute(statement)
        v1.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '1')")
        synthetic = 1 << 62
        v1.execute(
            "INSERT INTO sessions(id, question, account, seeds, limits, created_at) "
            "VALUES (1, 'q', 'work', ?, '{}', 1)",
            (f"[-1000000000100, 42, {synthetic}]",),
        )
        v1.execute(
            "INSERT INTO candidates(id, session_id, identity, kind, depth, status, created_at, "
            "probed_at) VALUES (1, 1, '@x', 'username', 1, 'pending_admission', 1, 5)"
        )
        for chat_id, via in ((-1000000000100, "link"), (42, "link"), (synthetic, "mention")):
            v1.execute(
                "INSERT INTO evidence(candidate_id, via, chat_id, msg_id, origin_key, found_at) "
                "VALUES (1, ?, ?, 3, 'k', 1)",
                (via, chat_id),
            )
        v1.execute(
            "INSERT INTO scans(session_id, chat_id, depth, msg_id, scanned_at) "
            "VALUES (1, -1000000000100, 0, 77, 1), (1, ?, 1, 5, 1)",
            (synthetic,),
        )

        assert research_db.migrate(v1) == research_db.SCHEMA_VERSION

        session = research_db.get_session(v1, 1)
        assert session is not None
        assert session.seeds == (ChatKey("", -1000000000100), ChatKey("work", 42))
        assert [e.chat for e in research_db.list_evidence(v1, 1)] == [
            ChatKey("", -1000000000100),
            ChatKey("work", 42),
            None,
        ]
        (cursor,) = research_db.list_scan_cursors(v1, 1)
        assert (cursor.chat, cursor.depth, cursor.lead_seq, cursor.index_id) == (
            ChatKey("", -1000000000100),
            0,
            0,
            None,
        ), "a msg_id cursor means nothing on the lead clock: the chat is read again"
        candidate = research_db.get_candidate(v1, 1)
        assert candidate is not None and candidate.requested_at == 5
    finally:
        v1.close()
