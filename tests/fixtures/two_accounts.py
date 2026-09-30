"""Two accounts' chats in one index, as a multi-account sync leaves them.

``default`` and ``work`` both reach the channel ``@hall`` — one shared row, two ``chat_access``
entries — whose discussion group is reached through the channel's link alone and so has no
access row of its own. Each account also has a private chat with Bob (peer :data:`BOB`): two
rows, the default one under the peer id and the work one under a synthetic id, holding
different histories whose message ids collide (both have a message 7), exactly as Telegram
numbers a private chat per account. Every chat mentions Brubank, so one query reaches all four.

:func:`load` runs the real pipeline (``upsert_messages`` → ``rebuild_for_chat`` →
``index_chat``) and stamps ``last_sync_at`` like :func:`tests.fixtures.chat_ru.load`.
"""

import sqlite3
from dataclasses import dataclass

from grepogram import db, index, units
from grepogram.models import AccountCfg, ChatRow, Config, MessageRow, Source
from tests.fakes import FakeWorld

HALL = -1001000000900
HALL_CHAT = -1001000000901
BOB = 42
BASE = 1_705_314_600  # 2024-01-15 10:30:00 UTC
SYNCED_AT = BASE + 3600

CFG = Config(
    accounts=[AccountCfg(name="work")],
    sources=[
        Source(chat="@hall", comments=True),
        Source(chat="@hall", account="work"),
        Source(chat=BOB),
        Source(chat=BOB, account="work"),
    ],
)


@dataclass(frozen=True, slots=True)
class TwoAccounts:
    hall: ChatRow
    hall_chat: ChatRow
    default_bob: ChatRow
    work_bob: ChatRow


def _msg(chat_id: int, msg_id: int, text: str, minutes: int = 0, **extra: object) -> MessageRow:
    fields: dict[str, object] = {
        "chat_id": chat_id,
        "msg_id": msg_id,
        "date": BASE + minutes * 60,
        "from_id": BOB,
        "from_name": "Bob",
        "text": text,
    }
    fields.update(extra)
    return MessageRow(**fields)  # type: ignore[arg-type]


def load(conn: sqlite3.Connection, synced_at: int | None = SYNCED_AT) -> TwoAccounts:
    """Store the four chats with their access and messages, indexed as a sync would."""
    hall = db.upsert_chat(
        conn,
        ChatRow(id=HALL, type="channel", title="Hall", username="hall", source_id="chat:@hall"),
    )
    hall_chat = db.upsert_chat(
        conn,
        ChatRow(
            id=HALL_CHAT,
            type="supergroup",
            title="Hall chat",
            source_id="chat:@hall",
            discussion_of=HALL,
        ),
    )
    default_bob = db.upsert_chat(
        conn, ChatRow(id=BOB, type="user", title="Bob", source_id=f"chat:{BOB}")
    )
    work_bob = db.upsert_chat(
        conn,
        ChatRow(id=BOB, type="user", title="Bob", scope="work", source_id=f"work/chat:{BOB}"),
        "work",
    )
    # each account's own hash for a peer, as a FakeWorld hands it to that account's client
    for chat, account in (
        (hall, "default"),
        (hall, "work"),
        (default_bob, "default"),
        (work_bob, "work"),
    ):
        db.set_chat_access(
            conn, chat.id, account, access_hash=FakeWorld.access_hash(account, chat.peer_id)
        )
    histories = {
        hall.id: [_msg(HALL, 1, "Brubank opens a new branch downtown", from_name="Hall")],
        hall_chat.id: [_msg(HALL_CHAT, 3, "The Brubank queue is long today", 5)],
        default_bob.id: [
            _msg(default_bob.id, 7, "My Brubank card arrived", 10),
            _msg(default_bob.id, 8, "Great, now order the second one", 11, reply_to_msg_id=7),
        ],
        work_bob.id: [_msg(work_bob.id, 7, "Brubank payroll moves to Friday", 20)],
    }
    cfg = Config()
    for chat in (hall, hall_chat, default_bob, work_bob):
        rows = histories[chat.id]
        ids = db.upsert_messages(conn, rows)
        delta = units.rebuild_for_chat(conn, chat, cfg, ids)
        index.index_chat(conn, chat, ids, delta)
        db.set_chat_progress(conn, chat.id, max(msg.msg_id for msg in rows), synced_at)
    return TwoAccounts(hall=hall, hall_chat=hall_chat, default_bob=default_bob, work_bob=work_bob)
