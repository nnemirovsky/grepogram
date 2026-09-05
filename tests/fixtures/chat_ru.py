"""A synthetic bilingual chat history for the search tests.

Two supergroups — the public Argentina chat (``@arg_chat``, links ``https://t.me/arg_chat/…``)
and a private Georgia chat (links ``https://t.me/c/…``) — hold 62 messages by five people about
bank accounts, SIM cards and visas, in Russian and English, with reply chains, a media-only
message and a caption. The messages sit in dated blocks hours or months apart, so windows,
threads and date filters all have something to bite on; :data:`CFG` cuts small windows and
threads so both continuation units and multi-window blocks appear.

:func:`load` runs the real pipeline — ``upsert_messages`` → ``rebuild_for_chat`` →
``index_chat`` — and stamps ``last_sync_at`` with :data:`SYNCED_AT`, so a search test sees what
a sync would have produced.

:data:`PARAPHRASE` names a pair of messages that say the same thing in the two languages without
one shared stem (``ВНЖ`` versus ``residence permit``): lexical search cannot connect them, which
is what the dense side is for once the fake embedder can bridge the two terms.
"""

import datetime as dt
import sqlite3
from dataclasses import dataclass

from grepogram import db, index, units
from grepogram.models import ChatRow, Config, MediaKind, MessageRow, Source, UnitsCfg

ARG_ID = -1001000000100
GEO_ID = -1001000000200
ARG = ChatRow(
    id=ARG_ID,
    type="supergroup",
    title="Argentina chat",
    username="arg_chat",
    source_id="folder:Argentina",
)
GEO = ChatRow(
    id=GEO_ID, type="supergroup", title="Грузия | Georgia chat", source_id=f"chat:{GEO_ID}"
)
CHATS = {ARG_ID: ARG, GEO_ID: GEO}
CFG = Config(
    units=UnitsCfg(window_gap_min=30, window_max_msgs=8, window_max_chars=900, thread_max_msgs=6),
    sources=[Source(folder="Argentina"), Source(chat=GEO_ID)],
)
SYNCED_AT = int(dt.datetime(2024, 7, 1, 12, 0, tzinfo=dt.UTC).timestamp())

USERS = {1: "Alice", 2: "Bob", 3: "Ольга", 4: "Дима", 5: "Maria"}


@dataclass(frozen=True, slots=True)
class Paraphrase:
    query_ru: str
    query_en: str
    ru_msg_id: int
    en_msg_id: int
    chat_id: int


PARAPHRASE = Paraphrase("ВНЖ", "residence permit", ru_msg_id=38, en_msg_id=26, chat_id=ARG_ID)


def _at(year: int, month: int, day: int, hour: int) -> int:
    return int(dt.datetime(year, month, day, hour, tzinfo=dt.UTC).timestamp())


# (msg_id, minute within the block, sender, text, reply_to); blocks are (start, chat, messages)
BLOCKS: list[tuple[int, int, list[tuple[int, int, int, str, int | None]]]] = [
    (
        _at(2024, 1, 15, 10),
        ARG_ID,
        [
            (1, 0, 3, "Всем привет! Подскажите, где открыть счёт без DNI? Только приехала.", None),
            (2, 2, 2, "Без DNI почти нигде. Сначала CUIT/CDI, потом Galicia или Santander.", 1),
            (3, 3, 1, "Brubank открыл мне счёт по паспорту, без DNI, но лимиты маленькие.", 1),
            (4, 5, 3, "А Brubank это нормальный банк или тоже приложение как Ualá?", 3),
            (5, 7, 1, "Это цифровой банк, карта Visa приходит по почте.", 4),
            (6, 10, 4, "Я в Galicia открывал счета для себя и жены, нужен DNI и справка.", 1),
            (7, 12, 2, "Santander ещё просит monotributo, без него отказали.", 6),
            (8, 15, 5, "Anyone knows if Santander branches in Palermo speak English?", None),
            (9, 17, 2, "Rarely. Bring a friend or use the app, the app is in Spanish only.", 8),
            (10, 20, 3, "Спасибо всем, попробую Brubank.", 3),
            (11, 25, 4, "", None),
            (12, 28, 4, "Вот скрин тарифов Galicia за обслуживание счёта.", None),
        ],
    ),
    (
        _at(2024, 1, 15, 14),
        ARG_ID,
        [
            (13, 0, 5, "Which SIM card works best in Buenos Aires? Claro or Movistar?", None),
            (14, 1, 1, "Claro has better coverage downtown, Movistar is cheaper for data.", 13),
            (15, 3, 4, "Симку Claro продают в любом киоске, регистрация по паспорту.", 13),
            (16, 5, 3, "А eSIM у Personal есть? Не хочу менять физическую сим-карту.", None),
            (17, 8, 2, "Personal делает eSIM в фирменных салонах, нужен DNI или паспорт.", 16),
            (18, 10, 3, "Спасибо, схожу в салон Personal на Santa Fe.", 17),
            (19, 12, 5, "Claro prepaid: 5 GB for a week, recharge in the app or at a kiosco.", 14),
            (20, 15, 1, "Recharge through Mercado Pago is easier than kiosks.", 19),
            (21, 18, 4, "Мобильный интернет у Movistar в провинции лучше, чем у Claro.", None),
            (22, 20, 2, "Согласен, за городом Movistar ловит лучше.", 21),
        ],
    ),
    (
        _at(2024, 3, 10, 9),
        ARG_ID,
        [
            (23, 0, 3, "Подала документы на ВНЖ (residencia temporaria) через RaDEX, ждём.", None),
            (24, 2, 4, "Сколько ждали precaria? У меня уже месяц ничего.", 23),
            (25, 4, 3, "Precaria выдали в тот же день, а residencia обещают через 3 месяца.", 24),
            (26, 6, 2, "Where can I apply for a residence permit with a work contract?", None),
            (27, 9, 1, "Migraciones, category trabajador; the employer registers you first.", 26),
            (28, 11, 2, "Thanks, does the precaria let me leave the country?", 27),
            (29, 13, 1, "Yes, precaria allows travel, just carry the printed permiso.", 28),
            (30, 16, 5, "Виза рантье (rentista) требует дохода около 5 минимальных зарплат.", None),
            (31, 18, 4, "Рентиста проще, чем рабочий контракт, но нужен апостиль на всё.", 30),
            (32, 21, 3, "Апостиль ставили в консульстве или дома?", 31),
            (33, 23, 4, "Дома, до отъезда; в Аргентине апостиль на них не поставить.", 32),
            (34, 26, 2, "Also: the DNI arrives by mail about a month after residencia.", None),
        ],
    ),
    (
        _at(2024, 6, 20, 18),
        ARG_ID,
        [
            (35, 0, 1, "Update: Galicia now opens accounts for tourists with a passport.", None),
            (36, 2, 4, "Правда? Раньше Galicia без DNI отказывал.", 35),
            (37, 4, 1, "Confirmed today at the Recoleta branch, they gave me a debit card.", 36),
            (38, 7, 3, "ВНЖ пришло! Residencia temporaria на 1 год, DNI ждать месяц.", None),
            (39, 9, 2, "Congrats! Did you use a gestor or did it yourself?", 38),
            (40, 11, 3, "Сама через RaDEX, без гестора.", 39),
            (41, 14, 5, "Movistar raised prepaid prices again, 5 GB now costs double.", None),
            (42, 16, 4, "У Claro тоже подорожало, но eSIM всё ещё бесплатный.", None),
        ],
    ),
    (
        _at(2024, 2, 5, 11),
        GEO_ID,
        [
            (1, 0, 4, "Кто открывал счёт в TBC как нерезидент? Какие документы?", None),
            (2, 2, 3, "TBC открыл по паспорту за час, но попросили справку с работы.", 1),
            (3, 4, 2, "Bank of Georgia was stricter, they asked for a residence certificate.", 1),
            (4, 6, 4, "А карту Visa выдают сразу?", 2),
            (5, 8, 3, "Да, карту выдали сразу, счета в лари и долларах.", 4),
            (6, 11, 1, "BoG rejected me twice, TBC in Vake approved without questions.", None),
            (7, 13, 5, "Same here, TBC is the friendliest bank for foreigners.", 6),
            (8, 16, 4, "Спасибо, пойду в TBC.", 2),
            (9, 20, 2, "Комиссия за перевод из Аргентины в TBC около 30 долларов.", None),
            (10, 22, 3, "Дороговато, лучше через crypto.", 9),
        ],
    ),
    (
        _at(2024, 2, 6, 15),
        GEO_ID,
        [
            (11, 0, 5, "Which SIM should I get in Tbilisi, Magti or Beeline?", None),
            (12, 2, 1, "Magti has the best coverage in the mountains, Beeline is cheaper.", 11),
            (13, 4, 4, "Симку Magti можно купить в аэропорту, регистрируют по паспорту.", 11),
            (14, 7, 3, "У Silknet тоже есть eSIM, оформляла онлайн.", None),
            (15, 9, 2, "Russians get a visa-free year in Georgia, no residence permit.", None),
            (16, 11, 4, "Безвиз на год, но ВНЖ нужен для покупки машины.", 15),
            (17, 13, 1, "Residence permit through property purchase needs 100k USD now.", 16),
            (18, 16, 3, "Раньше было 35 тысяч, подняли в прошлом году.", 17),
            (19, 19, 5, "Visa run to Armenia resets the year, takes one day.", 15),
            (20, 21, 2, "Yes, but the border sometimes asks questions after several runs.", 19),
        ],
    ),
]
MEDIA_ONLY = (ARG_ID, 11)
CAPTIONED = (ARG_ID, 12)


def messages(chat_id: int | None = None) -> list[MessageRow]:
    """Every fixture message (of one chat) in block order."""
    rows: list[MessageRow] = []
    for start, block_chat, entries in BLOCKS:
        if chat_id is not None and block_chat != chat_id:
            continue
        for msg_id, minute, sender, text, reply_to in entries:
            media: MediaKind | None = (
                "photo" if (block_chat, msg_id) in (MEDIA_ONLY, CAPTIONED) else None
            )
            rows.append(
                MessageRow(
                    chat_id=block_chat,
                    msg_id=msg_id,
                    date=start + minute * 60,
                    from_id=sender,
                    from_name=USERS[sender],
                    reply_to_msg_id=reply_to,
                    text=text,
                    media_kind=media,
                )
            )
    return rows


def message(chat_id: int, msg_id: int) -> MessageRow:
    return next(msg for msg in messages(chat_id) if msg.msg_id == msg_id)


@dataclass(frozen=True, slots=True)
class Loaded:
    chats: dict[int, ChatRow]
    row_ids: dict[int, dict[int, int]]
    """``chat_id → msg_id → messages.id``."""


def load(conn: sqlite3.Connection, cfg: Config = CFG, synced_at: int | None = SYNCED_AT) -> Loaded:
    """Store both chats, rebuild their units and index everything, as a sync would."""
    row_ids: dict[int, dict[int, int]] = {}
    for chat in CHATS.values():
        stored = db.upsert_chat(conn, chat)
        rows = messages(chat.id)
        ids = db.upsert_messages(conn, rows)
        delta = units.rebuild_for_chat(conn, stored, cfg, ids)
        index.index_chat(conn, stored, ids, delta)
        db.set_chat_progress(conn, chat.id, max(msg.msg_id for msg in rows), synced_at)
        row_ids[chat.id] = {msg.msg_id: row_id for msg, row_id in zip(rows, ids, strict=True)}
    return Loaded(
        chats={chat_id: db.get_chat(conn, chat_id) or chat for chat_id, chat in CHATS.items()},
        row_ids=row_ids,
    )
