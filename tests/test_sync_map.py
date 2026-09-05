import datetime as dt
import logging

import pytest
from telethon.tl import types

from grepogram import sync
from grepogram.models import ChatRow, MessageRow, UserRow
from tests.fakes import make_channel, make_group, make_message, make_user
from tests.fixtures import tl

ALICE = make_user(1, "Alice", "Liddell", username="alice")
BOB = make_user(2, "Bob")
DELETED = make_user(3, "", None)
HELPER = make_user(4, "Helper", bot=True, username="helper_bot")
OLD_GROUP = make_group(10, "Old group")
ARG_ENTITY = make_channel(100, "Argentina chat", username="arg_chat", megagroup=True, forum=True)
NEWS_ENTITY = make_channel(200, "News", username="news")

ARG = ChatRow(id=-1000000000100, type="supergroup", title="Argentina chat", is_forum=True)
NEWS = ChatRow(id=-1000000000200, type="channel", title="News", username="news")
DM = ChatRow(id=1, type="user", title="Alice Liddell", username="alice")
ME = UserRow(id=42, display_name="Me Myself", username="me")

USERS = sync.collect_users([ALICE, BOB, DELETED, HELPER, OLD_GROUP, ARG_ENTITY, NEWS_ENTITY])
NAMES = {user_id: user.display_name for user_id, user in USERS.items() if user.display_name}


def _map(msg: object, chat: ChatRow = ARG, names: dict[int, str] | None = None) -> MessageRow:
    row = sync.map_message(msg, chat, NAMES if names is None else names)
    assert row is not None
    return row


# --- users -----------------------------------------------------------------------------------


def test_collect_users_users_and_chats_by_marked_id() -> None:
    assert USERS[1] == UserRow(id=1, display_name="Alice Liddell", username="alice")
    assert USERS[2] == UserRow(id=2, display_name="Bob", username=None)
    assert USERS[4] == UserRow(id=4, display_name="Helper", username="helper_bot")
    assert USERS[-10] == UserRow(id=-10, display_name="Old group", username=None)
    assert USERS[ARG.id] == UserRow(id=ARG.id, display_name="Argentina chat", username="arg_chat")
    assert USERS[NEWS.id] == UserRow(id=NEWS.id, display_name="News", username="news")


def test_collect_users_deleted_account_has_no_name() -> None:
    assert USERS[3] == UserRow(id=3, display_name=None, username=None)
    assert 3 not in NAMES


def test_collect_users_skips_non_entities() -> None:
    assert sync.collect_users([None, types.UserEmpty(9), types.PeerUser(1), "x"]) == {}


def test_collect_users_forbidden_chats_and_aliases() -> None:
    forbidden = types.ChannelForbidden(300, 300, "Gone", megagroup=True)
    aliased = types.Channel(
        id=301,
        title="Aliased",
        photo=types.ChatPhotoEmpty(),
        date=None,
        usernames=[types.Username("main_alias", active=True)],
        access_hash=301,
    )
    users = sync.collect_users([forbidden, aliased])
    assert users[-1000000000300] == UserRow(id=-1000000000300, display_name="Gone", username=None)
    assert users[-1000000000301].username == "main_alias"


# --- sender_of -------------------------------------------------------------------------------


def test_sender_from_id_user() -> None:
    assert sync.sender_of(tl.message(ARG.id, 1, "x", sender=1), NAMES) == (1, "Alice Liddell")


def test_sender_from_id_unknown_falls_back_to_id() -> None:
    assert sync.sender_of(tl.message(ARG.id, 1, "x", sender=77), NAMES) == (77, "id77")


def test_sender_anonymous_admin_is_the_group() -> None:
    msg = tl.message(ARG.id, 1, "x", sender=ARG_ENTITY)
    assert sync.sender_of(msg, NAMES) == (ARG.id, "Argentina chat")


def test_sender_send_as_channel() -> None:
    msg = tl.message(ARG.id, 1, "x", sender=NEWS_ENTITY)
    assert sync.sender_of(msg, NAMES) == (NEWS.id, "News")


def test_sender_channel_post_is_the_channel() -> None:
    msg = tl.channel_post(NEWS.id, 1, "x")
    assert sync.sender_of(msg, NAMES) == (NEWS.id, "News")
    assert sync.sender_of(msg, {}) == (NEWS.id, f"id{NEWS.id}")


def test_sender_channel_post_signature_when_channel_unknown() -> None:
    msg = tl.channel_post(NEWS.id, 1, "x", post_author="Ivan")
    assert sync.sender_of(msg, {}) == (NEWS.id, "Ivan")
    assert sync.sender_of(msg, NAMES) == (NEWS.id, "News")


def test_sender_incoming_private_message_is_the_peer() -> None:
    assert sync.sender_of(tl.message(1, 1, "x"), NAMES) == (1, "Alice Liddell")


def test_sender_outgoing_private_message_is_me() -> None:
    msg = tl.message(1, 1, "x", out=True)
    assert sync.sender_of(msg, NAMES) == (None, "me")
    assert sync.sender_of(msg, NAMES, me=ME) == (42, "Me Myself")
    assert sync.sender_of(msg, NAMES, me=UserRow(id=42)) == (42, "me")


def test_sender_me_names_own_from_id_when_absent_from_names() -> None:
    msg = tl.message(ARG.id, 1, "x", sender=42, out=True)
    assert sync.sender_of(msg, NAMES) == (42, "id42")
    assert sync.sender_of(msg, NAMES, me=ME) == (42, "Me Myself")
    assert sync.sender_of(msg, {42: "Listed"}, me=ME) == (42, "Listed")


# --- map_message: fixture kinds --------------------------------------------------------------


def test_text_message() -> None:
    when = dt.datetime(2025, 3, 4, 5, 6, 7, tzinfo=dt.UTC)
    edited = when + dt.timedelta(minutes=2)
    msg = tl.text_message(ARG.id, 5, "hello", sender=1, date=when, edit_date=edited)
    assert _map(msg) == MessageRow(
        chat_id=ARG.id,
        msg_id=5,
        date=int(when.timestamp()),
        edit_date=int(edited.timestamp()),
        from_id=1,
        from_name="Alice Liddell",
        text="hello",
    )


def test_message_built_without_client_maps() -> None:
    msg = tl.text_message(ARG.id, 5, "hello", sender=1)
    assert msg.client is None
    row = _map(msg)
    assert (row.chat_id, row.msg_id, row.text, row.from_name) == (
        ARG.id,
        5,
        "hello",
        "Alice Liddell",
    )
    assert row.date == int(tl.at(5).timestamp())


def test_photo_with_caption() -> None:
    row = _map(tl.photo_message(ARG.id, 6, "look", sender=1))
    assert (row.text, row.media_kind, row.media_filename) == ("look", "photo", None)


def test_photo_without_caption_keeps_empty_text() -> None:
    row = _map(tl.photo_message(ARG.id, 6, sender=1))
    assert (row.text, row.media_kind) == ("", "photo")


def test_voice() -> None:
    row = _map(tl.voice_message(ARG.id, 7, sender=1))
    assert (row.text, row.media_kind, row.media_filename) == ("", "voice", None)


def test_document_with_filename() -> None:
    row = _map(tl.document_message(ARG.id, 8, "visa.pdf", "the form", sender=1))
    assert (row.text, row.media_kind, row.media_filename) == ("the form", "document", "visa.pdf")


def test_reply() -> None:
    row = _map(tl.reply_message(ARG.id, 9, "yes", reply_to=5, sender=2))
    assert (row.reply_to_msg_id, row.topic_id, row.from_name) == (5, None, "Bob")


def test_forum_topic_root_is_not_a_reply() -> None:
    row = _map(tl.topic_message(ARG.id, 10, "in topic", topic_id=3, sender=1))
    assert (row.reply_to_msg_id, row.topic_id) == (None, 3)


def test_forum_reply_inside_topic() -> None:
    row = _map(tl.reply_message(ARG.id, 11, "re", reply_to=10, topic_id=3, sender=1))
    assert (row.reply_to_msg_id, row.topic_id) == (10, 3)


def test_forwarded() -> None:
    row = _map(tl.forwarded_message(ARG.id, 12, "fwd", origin=2, sender=1))
    assert (row.fwd_from, row.from_id, row.text) == ("Bob", 1, "fwd")


def test_service_message_skipped() -> None:
    assert sync.map_message(tl.service_message(ARG.id, 13, sender=1), ARG, NAMES) is None
    joined = tl.service_message(ARG.id, 14, types.MessageActionChatAddUser([2]), sender=1)
    assert sync.map_message(joined, ARG, NAMES) is None


def test_reactions_total() -> None:
    msg = tl.text_message(ARG.id, 15, "x", sender=1, reactions=tl.reactions({"👍": 3, "🔥": 2}))
    assert _map(msg).reactions_total == 5
    assert _map(tl.text_message(ARG.id, 16, "x", sender=1)).reactions_total == 0
    empty = tl.text_message(ARG.id, 17, "x", sender=1, reactions=types.MessageReactions([]))
    assert _map(empty).reactions_total == 0


def test_channel_post() -> None:
    row = _map(tl.channel_post(NEWS.id, 18, "breaking", post_author="Ivan"), NEWS, {})
    assert (row.chat_id, row.from_id, row.from_name, row.text) == (
        NEWS.id,
        NEWS.id,
        "News",
        "breaking",
    )
    assert _map(tl.channel_post(NEWS.id, 18, "breaking"), NEWS, {NEWS.id: "News!"}).from_name == (
        "News!"
    )


# --- map_message: senders in context ---------------------------------------------------------


def test_private_chat_incoming_and_outgoing() -> None:
    incoming = _map(tl.message(1, 1, "hi"), DM)
    assert (incoming.from_id, incoming.from_name) == (1, "Alice Liddell")
    outgoing = _map(tl.message(1, 2, "hey", out=True), DM)
    assert (outgoing.from_id, outgoing.from_name) == (None, "me")
    named = sync.map_message(tl.message(1, 2, "hey", out=True), DM, NAMES, me=ME)
    assert named is not None and (named.from_id, named.from_name) == (42, "Me Myself")


def test_chat_title_names_the_chat_itself_when_absent_from_names() -> None:
    row = _map(tl.message(ARG.id, 1, "x", sender=ARG_ENTITY), ARG, {})
    assert (row.from_id, row.from_name) == (ARG.id, "Argentina chat")
    untitled = ChatRow(id=ARG.id, type="supergroup")
    row = _map(tl.message(ARG.id, 1, "x", sender=ARG_ENTITY), untitled, {})
    assert row.from_name == f"id{ARG.id}"


def test_row_is_stored_under_the_given_chat() -> None:
    discussion = ChatRow(
        id=-1000000000201, type="supergroup", title="News chat", discussion_of=NEWS.id
    )
    comment = tl.reply_message(discussion.id, 40, "comment", reply_to=39, sender=1)
    row = _map(comment, discussion)
    assert (row.chat_id, row.msg_id, row.reply_to_msg_id) == (discussion.id, 40, 39)


# --- media kinds -----------------------------------------------------------------------------


def _doc(*attributes: object, **flags: bool) -> types.MessageMediaDocument:
    return types.MessageMediaDocument(document=tl.document(*attributes), **flags)


@pytest.mark.parametrize(
    ("media", "kind", "filename"),
    [
        (None, None, None),
        (types.MessageMediaEmpty(), None, None),
        (types.MessageMediaPhoto(photo=tl.photo()), "photo", None),
        (tl.voice_message(ARG.id, 1).media, "voice", None),
        (tl.video_message(ARG.id, 1).media, "video", "clip.mp4"),
        (tl.video_note_message(ARG.id, 1).media, "video_note", None),
        (tl.audio_message(ARG.id, 1).media, "audio", "song.mp3"),
        (tl.document_message(ARG.id, 1, "a.zip").media, "document", "a.zip"),
        (tl.sticker_message(ARG.id, 1).media, "sticker", None),
        (tl.sticker_message(ARG.id, 1, video=True).media, "sticker", None),
        (tl.gif_message(ARG.id, 1).media, "video", "funny.gif"),
        (
            _doc(types.DocumentAttributeCustomEmoji("x", types.InputStickerSetEmpty())),
            "sticker",
            None,
        ),
        (_doc(), "document", None),
        (types.MessageMediaDocument(document=None), "document", None),
        (types.MessageMediaDocument(document=types.DocumentEmpty(1)), "document", None),
        (types.MessageMediaDocument(document=types.DocumentEmpty(1), voice=True), "voice", None),
        (types.MessageMediaDocument(document=None, round=True, video=True), "video_note", None),
        (types.MessageMediaDocument(document=None, video=True), "video", None),
        (tl.poll_message(ARG.id, 1, "q", ["a"]).media, "poll", None),
        (tl.contact_message(ARG.id, 1, "Ann").media, "contact", None),
        (tl.location_message(ARG.id, 1).media, "location", None),
        (types.MessageMediaGeoLive(geo=tl.geo_point(), period=60), "location", None),
        (tl.venue_message(ARG.id, 1, "Cafe", "Street 1").media, "location", None),
        (tl.webpage_message(ARG.id, 1, "see").media, "webpage", None),
        (types.MessageMediaDice(value=3, emoticon="🎲"), "other", None),
        (types.MessageMediaUnsupported(), "other", None),
        (types.MessageMediaStory(peer=types.PeerUser(1), id=1), "other", None),
    ],
)
def test_media_of(media: object, kind: str | None, filename: str | None) -> None:
    assert sync.media_of(media) == (kind, filename)


def test_media_kinds_through_map_message() -> None:
    assert _map(tl.video_message(ARG.id, 1, "cap", sender=1)).media_kind == "video"
    assert _map(tl.video_note_message(ARG.id, 2, sender=1)).media_kind == "video_note"
    assert _map(tl.audio_message(ARG.id, 3, sender=1)).media_kind == "audio"
    assert _map(tl.sticker_message(ARG.id, 4, sender=1)).media_kind == "sticker"
    assert _map(tl.webpage_message(ARG.id, 5, "see https://example.com", sender=1)).text == (
        "see https://example.com"
    )


# --- media text ------------------------------------------------------------------------------


def test_poll_text_is_question_and_answers() -> None:
    row = _map(tl.poll_message(ARG.id, 1, "Which bank?", ["Galicia", "Santander"], sender=1))
    assert (row.media_kind, row.text) == ("poll", "Which bank?\nGalicia\nSantander")


def test_poll_with_plain_string_question() -> None:
    poll = types.Poll(id=1, question="Old?", answers=[types.PollAnswer("yes", b"0")], hash=0)
    media = types.MessageMediaPoll(poll=poll, results=types.PollResults())
    assert sync.media_text(media) == "Old?\nyes"


def test_venue_and_contact_text() -> None:
    venue = _map(tl.venue_message(ARG.id, 1, "Cafe Tortoni", "Av. de Mayo 825", sender=1))
    assert (venue.media_kind, venue.text) == ("location", "Cafe Tortoni\nAv. de Mayo 825")
    contact = _map(tl.contact_message(ARG.id, 2, "Ann", "Lee", sender=1))
    assert (contact.media_kind, contact.text) == ("contact", "Ann Lee")
    assert _map(tl.contact_message(ARG.id, 3, "Ann", sender=1)).text == "Ann"
    assert _map(tl.location_message(ARG.id, 4, sender=1)).text == ""


def test_caption_wins_over_media_text() -> None:
    media = tl.venue_message(ARG.id, 1, "Cafe", "Street").media
    assert _map(tl.message(ARG.id, 1, "meet here", media=media, sender=1)).text == "meet here"


# --- replies ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, (None, None)),
        (tl.reply_header(5), (5, None)),
        (tl.reply_header(None, topic_id=3), (None, 3)),
        (tl.reply_header(10, topic_id=3), (10, 3)),
        (types.MessageReplyHeader(forum_topic=True, reply_to_top_id=3), (None, 3)),
        (types.MessageReplyHeader(forum_topic=True), (None, None)),
        (tl.reply_header(5, reply_to_peer=ARG.id), (5, None)),
        (tl.reply_header(5, reply_to_peer=NEWS.id), (None, None)),
        (tl.reply_header(5, topic_id=3, reply_to_peer=NEWS.id), (None, 3)),
        (types.MessageReplyStoryHeader(peer=types.PeerUser(1), story_id=7), (None, None)),
    ],
)
def test_reply_of(header: object, expected: tuple[int | None, int | None]) -> None:
    assert sync.reply_of(header, ARG.id) == expected


def test_story_reply_is_not_a_reply() -> None:
    header = types.MessageReplyStoryHeader(peer=types.PeerUser(1), story_id=7)
    row = _map(tl.message(ARG.id, 1, "nice story", reply_to=header, sender=1))
    assert (row.reply_to_msg_id, row.topic_id) == (None, None)


# --- forwards --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        (tl.forward_header(1), "Alice Liddell"),
        (tl.forward_header(NEWS_ENTITY, channel_post=7), "News"),
        (tl.forward_header(NEWS_ENTITY, channel_post=7, post_author="Ivan"), "News"),
        (tl.forward_header(77), "id77"),
        (tl.forward_header(77, origin_name="Hidden"), "Hidden"),
        (tl.forward_header(origin_name="Hidden User"), "Hidden User"),
        (tl.forward_header(post_author="Ivan"), "Ivan"),
        (tl.forward_header(), "unknown"),
    ],
)
def test_forward_of(header: object, expected: str | None) -> None:
    assert sync.forward_of(header, NAMES) == expected


# --- dates and skips -------------------------------------------------------------------------


def test_epoch_naive_is_utc() -> None:
    aware = dt.datetime(2025, 6, 1, 12, 0, tzinfo=dt.UTC)
    assert sync.epoch(aware) == 1748779200
    assert sync.epoch(aware.replace(tzinfo=None)) == 1748779200
    plus_three = dt.datetime(2025, 6, 1, 15, 0, tzinfo=dt.timezone(dt.timedelta(hours=3)))
    assert sync.epoch(plus_three) == 1748779200


def test_message_without_date_skipped(caplog: pytest.LogCaptureFixture) -> None:
    msg = types.Message(id=1, peer_id=types.PeerChannel(100), message="x")
    with caplog.at_level(logging.DEBUG, logger="grepogram.sync"):
        assert sync.map_message(msg, ARG, NAMES) is None
    assert "no date" in caplog.text


def test_message_empty_and_foreign_objects_skipped() -> None:
    assert sync.map_message(types.MessageEmpty(1, types.PeerChannel(100)), ARG, NAMES) is None
    assert sync.map_message(None, ARG, NAMES) is None
    assert sync.map_message("text", ARG, NAMES) is None


def test_make_message_uses_fixture_builder() -> None:
    msg = make_message(ARG.id, 3, "x")
    assert msg.date == tl.at(3)
    assert _map(msg).text == "x"
