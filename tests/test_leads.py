import pytest

from grepogram import leads
from grepogram.leads import LeadTarget

CHANNEL_42 = -1000000000042


@pytest.mark.parametrize(
    ("value", "target"),
    [
        # usernames, every host and spelling, lowercased
        ("https://t.me/News_Chat", "@news_chat"),
        ("http://t.me/news_chat", "@news_chat"),
        ("t.me/news_chat", "@news_chat"),
        ("https://www.t.me/news_chat", "@news_chat"),
        ("https://telegram.me/news_chat", "@news_chat"),
        ("https://telegram.dog/news_chat", "@news_chat"),
        ("HTTPS://T.ME/news_chat", "@news_chat"),
        ("https://news_chat.t.me", "@news_chat"),
        ("https://t.me/news_chat?start=abc", "@news_chat"),
        ("https://t.me/s/news_chat", "@news_chat"),
        ("https://t.me/news_chat/", "@news_chat"),
        ("https://t.me/news_chat.", "@news_chat"),
        ("@News_Chat", "@news_chat"),
        ("tg://resolve?domain=news_chat", "@news_chat"),
        ("tg:resolve?domain=news_chat", "@news_chat"),
        ("https://t.me/abcd", "@abcd"),
        # posts of a public chat, a forum topic's included
        ("https://t.me/news_chat/123", "@news_chat/123"),
        ("https://t.me/news_chat/123?comment=5", "@news_chat/123"),
        ("https://t.me/s/news_chat/123", "@news_chat/123"),
        ("https://t.me/forum_chat/7/123", "@forum_chat/123"),
        ("tg://resolve?domain=news_chat&post=123", "@news_chat/123"),
        # private posts: the bare id a t.me/c/ link carries
        ("https://t.me/c/42/9", "c/42/9"),
        ("https://t.me/c/42/3/9", "c/42/9"),
        ("tg://privatepost?channel=42&post=9", "c/42/9"),
        # invites
        ("https://t.me/+AbC-d_12345", "+AbC-d_12345"),
        ("https://t.me/joinchat/AbC-d_12345", "+AbC-d_12345"),
        ("tg://join?invite=AbC-d_12345", "+AbC-d_12345"),
        # shared folders
        ("https://t.me/addlist/XyZ_123", "addlist/XyZ_123"),
        ("tg://addlist?slug=XyZ_123", "addlist/XyZ_123"),
        # Telegram's routes open whatever their case; the hash and the slug keep theirs
        ("https://t.me/JoinChat/AbC-d_12345", "+AbC-d_12345"),
        ("https://t.me/AddList/XyZ_123", "addlist/XyZ_123"),
        ("https://t.me/C/42/9", "c/42/9"),
        ("https://t.me/S/News_Chat/123", "@news_chat/123"),
        # peers by id
        ("https://t.me/c/42", f"peer:{CHANNEL_42}"),
        ("tg://user?id=777", "peer:777"),
        ("peer:-77", "peer:-77"),
    ],
)
def test_normalize(value: str, target: str) -> None:
    lead = leads.normalize(value)
    assert lead is not None
    assert lead.target == target


@pytest.mark.parametrize(
    "value",
    [
        "",
        "https://example.com/news_chat",
        "https://t.me.evil.com/news_chat",
        "https://nott.me/news_chat",
        "https://t.me/",
        "https://t.me/+15551234567",
        "https://t.me/addstickers/Animals",
        "https://t.me/share/url?url=x",
        "https://t.me/proxy?server=1.2.3.4",
        "https://t.me/joinchat",
        "https://t.me/c/abc/1",
        "https://t.me/abc",
        "https://t.me/1news",
        "ftp://t.me/news_chat",
        "tg://resolve?phone=15551234567",
        "tg://msg_url?url=x",
        "@abc",
        "@news_chat/abc",
        "c/42",
        "peer:x",
        "peer:0",
        "mailto:someone@t.me",
        "https://someone@t.me/news_chat",
    ],
)
def test_normalize_refuses_what_names_no_telegram_destination(value: str) -> None:
    assert leads.normalize(value) is None


def test_normalize_fills_what_a_target_names() -> None:
    assert leads.normalize("https://t.me/News/5") == LeadTarget(
        kind="post", target="@news/5", username="news", msg_id=5
    )
    assert leads.normalize("https://t.me/c/42/9") == LeadTarget(
        kind="private_post", target="c/42/9", peer_id=CHANNEL_42, msg_id=9
    )
    assert leads.normalize("https://t.me/+Hash_1") == LeadTarget(
        kind="invite", target="+Hash_1", invite_hash="Hash_1"
    )
    assert leads.normalize("t.me/addlist/Slug") == LeadTarget(
        kind="addlist", target="addlist/Slug", slug="Slug"
    )
    assert leads.normalize("@news") == LeadTarget(kind="username", target="@news", username="news")
    assert leads.normalize("peer:12") == LeadTarget(kind="peer", target="peer:12", peer_id=12)


def test_private_channel_mark_is_arithmetic() -> None:
    """A channel id below ten digits leaves zeros right behind the -100 prefix; gluing the
    prefix onto the digits would name another peer."""
    lead = leads.normalize("https://t.me/c/5/1")
    assert lead is not None and lead.peer_id == -1000000000005


@pytest.mark.parametrize(
    "value",
    [
        "https://t.me/news_chat",
        "https://t.me/news_chat/123",
        "https://t.me/c/42/9",
        "https://t.me/c/42",
        "https://t.me/+AbC-d_12345",
        "https://t.me/addlist/XyZ_123",
        "tg://user?id=777",
    ],
)
def test_normalize_reads_its_own_targets_back(value: str) -> None:
    lead = leads.normalize(value)
    assert lead is not None
    assert leads.normalize(lead.target) == lead


def test_invite_hash_and_slug_keep_their_case() -> None:
    first = leads.normalize("https://t.me/+AbCdEf")
    second = leads.normalize("https://t.me/+abcdef")
    assert first is not None and second is not None
    assert first.target != second.target


def test_text_leads_finds_visible_links_and_mentions() -> None:
    text = (
        "Join https://t.me/news_chat, see t.me/news_chat/5 (and tg://join?invite=Hash_1). "
        "Ask @helper_bot or @Alice_L. Folder: telegram.me/addlist/Slug! "
        "Not these: https://example.com/x, mail me@example.com, rot.me/abc, @abc."
    )
    assert leads.text_leads(text) == (
        ("link", "+Hash_1"),
        ("link", "@news_chat"),
        ("link", "@news_chat/5"),
        ("link", "addlist/Slug"),
        ("mention", "@alice_l"),
        ("mention", "@helper_bot"),
    )


def test_text_leads_deduplicates_and_is_empty_without_leads() -> None:
    assert leads.text_leads("t.me/news https://t.me/News @news @NEWS") == (
        ("link", "@news"),
        ("mention", "@news"),
    )
    assert leads.text_leads("nothing to see at https://example.com") == ()
    assert leads.text_leads("") == ()
