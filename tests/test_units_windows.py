import random

import pytest

from grepogram import units
from grepogram.models import MessageRow, UnitRow, UnitsCfg

BASE = 1_705_314_600  # 2024-01-15 10:30:00 UTC
CHAT = -1000000000100
CFG = UnitsCfg(window_gap_min=30, window_max_msgs=3, window_max_chars=80, thread_max_msgs=40)


def _msg(msg_id: int, minutes: int = 0, text: str | None = None, **overrides: object) -> MessageRow:
    fields: dict[str, object] = {
        "chat_id": CHAT,
        "msg_id": msg_id,
        "date": BASE + minutes * 60,
        "from_id": 1,
        "from_name": "Alice",
        "text": f"message {msg_id}" if text is None else text,
    }
    fields.update(overrides)
    return MessageRow(**fields)  # type: ignore[arg-type]


def _run(messages: list[MessageRow], cfg: UnitsCfg = CFG) -> list[UnitRow]:
    return units.cut_windows(messages, cfg, CHAT)


def _ids(windows: list[UnitRow]) -> list[list[int]]:
    return [window.msg_ids for window in windows]


# --- render_line -----------------------------------------------------------------------------


def test_render_line_text() -> None:
    assert units.render_line(_msg(1, text="hello")) == "[2024-01-15 10:30] Alice: hello"


def test_render_line_stamp_is_utc_and_minute_precise() -> None:
    line = units.render_line(_msg(1, minutes=95, text="x"))
    assert line.startswith("[2024-01-15 12:05] ")


def test_render_line_strips_text_but_keeps_inner_newlines() -> None:
    line = units.render_line(_msg(1, text="  first\nsecond  \n"))
    assert line == "[2024-01-15 10:30] Alice: first\nsecond"


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"text": "", "media_kind": "photo"}, "[photo]"),
        ({"text": "", "media_kind": "voice"}, "[voice]"),
        ({"text": "   ", "media_kind": "video_note"}, "[video_note]"),
        ({"text": "", "media_kind": "document", "media_filename": "cv.pdf"}, "[document: cv.pdf]"),
        ({"text": "", "media_kind": None}, "[empty]"),
        ({"text": "caption", "media_kind": "photo"}, "caption"),
    ],
)
def test_render_line_placeholders(overrides: dict[str, object], expected: str) -> None:
    line = units.render_line(_msg(1, **overrides))
    assert line == f"[2024-01-15 10:30] Alice: {expected}"


@pytest.mark.parametrize(
    ("from_id", "from_name", "expected"),
    [
        (1, "Alice", "Alice"),
        (7, None, "id7"),
        (7, "", "id7"),
        (-1000000000200, None, "id-1000000000200"),
        (None, None, "unknown"),
    ],
)
def test_render_line_sender_fallbacks(
    from_id: int | None, from_name: str | None, expected: str
) -> None:
    line = units.render_line(_msg(1, from_id=from_id, from_name=from_name, text="x"))
    assert line == f"[2024-01-15 10:30] {expected}: x"


# --- cut_windows -----------------------------------------------------------------------------


def test_cut_windows_empty_input() -> None:
    assert _run([]) == []


def test_cut_windows_single_message() -> None:
    windows = _run([_msg(5, minutes=2, text="solo")])
    assert windows == [
        UnitRow(
            chat_id=CHAT,
            topic_id=None,
            kind="window",
            msg_id_start=5,
            msg_id_end=5,
            msg_ids=[5],
            date_start=BASE + 120,
            date_end=BASE + 120,
            text="[2024-01-15 10:32] Alice: solo",
        )
    ]
    assert windows[0].id is None
    assert windows[0].dirty is True
    assert windows[0].embedded_model is None


def test_cut_windows_gap_cut() -> None:
    messages = [_msg(1, 0), _msg(2, 10), _msg(3, 20), _msg(4, 51), _msg(5, 60)]
    windows = _run(messages)
    assert _ids(windows) == [[1, 2, 3], [4, 5]]
    assert (windows[0].date_start, windows[0].date_end) == (BASE, BASE + 20 * 60)
    assert (windows[1].date_start, windows[1].date_end) == (BASE + 51 * 60, BASE + 60 * 60)


def test_cut_windows_gap_equal_to_limit_stays_open() -> None:
    assert _ids(_run([_msg(1, 0), _msg(2, 30)])) == [[1, 2]]
    assert _ids(_run([_msg(1, 0), _msg(2, 31)])) == [[1], [2]]


def test_cut_windows_gap_is_between_consecutive_messages() -> None:
    windows = _run([_msg(i, 25 * i) for i in range(1, 4)])
    assert _ids(windows) == [[1, 2, 3]]


def test_cut_windows_count_cut() -> None:
    windows = _run([_msg(i, i) for i in range(1, 8)])
    assert _ids(windows) == [[1, 2, 3], [4, 5, 6], [7]]
    assert [(w.msg_id_start, w.msg_id_end) for w in windows] == [(1, 3), (4, 6), (7, 7)]


def test_cut_windows_char_cut() -> None:
    long_text = "x" * 50
    cfg = UnitsCfg(window_gap_min=30, window_max_msgs=100, window_max_chars=120)
    windows = _run([_msg(i, i, text=long_text) for i in range(1, 6)], cfg)
    assert _ids(windows) == [[1, 2], [3, 4], [5]]
    for window in windows[:-1]:
        assert len(window.text) >= 120
        assert len(window.text.split("\n")[0]) < 120


def test_cut_windows_oversized_message_is_its_own_window() -> None:
    cfg = UnitsCfg(window_gap_min=30, window_max_msgs=100, window_max_chars=40)
    messages = [
        _msg(1, 0, text="a" * 500),
        _msg(2, 1, text="b" * 500),
        _msg(3, 2, text="short"),
        _msg(4, 3, text="short"),
    ]
    windows = _run(messages, cfg)
    assert _ids(windows) == [[1], [2], [3, 4]]
    assert len(windows[0].text) > 500


def test_cut_windows_char_budget_counts_rendered_lines() -> None:
    cfg = UnitsCfg(window_gap_min=30, window_max_msgs=100, window_max_chars=50)
    line = units.render_line(_msg(1, 0, text="ab"))
    assert len(line) < 50 <= 2 * len(line) + 1
    windows = _run([_msg(i, i, text="ab") for i in range(1, 5)], cfg)
    assert _ids(windows) == [[1, 2], [3, 4]]


def test_cut_windows_text_is_rendered_lines() -> None:
    messages = [_msg(1, 0, text="hi"), _msg(2, 1, text="", media_kind="photo", from_name="Bob")]
    (window,) = _run(messages)
    assert window.text == "[2024-01-15 10:30] Alice: hi\n[2024-01-15 10:31] Bob: [photo]"


def test_cut_windows_orders_chronologically_and_is_deterministic() -> None:
    messages = [_msg(i, i * 2) for i in range(1, 12)]
    expected = _run(messages)
    shuffled = list(messages)
    random.Random(7).shuffle(shuffled)
    assert _run(shuffled) == expected
    assert _run(list(reversed(messages))) == expected
    assert _run(messages) == expected


def test_cut_windows_msg_id_range_is_min_max_of_window() -> None:
    windows = _run([_msg(3, 0), _msg(1, 1), _msg(2, 2)])
    assert _ids(windows) == [[3, 1, 2]]
    assert (windows[0].msg_id_start, windows[0].msg_id_end) == (1, 3)


def test_cut_windows_stamps_chat_and_topic() -> None:
    windows = units.cut_windows([_msg(1, topic_id=42), _msg(2, 1, topic_id=42)], CFG, 777, 42)
    assert [(w.chat_id, w.topic_id, w.kind) for w in windows] == [(777, 42, "window")]


def test_cut_windows_respects_every_limit() -> None:
    rng = random.Random(1)
    messages = []
    minute = 0
    for msg_id in range(1, 200):
        minute += rng.choice([0, 1, 5, 29, 30, 31, 90])
        messages.append(_msg(msg_id, minute, text="w" * rng.randint(0, 60)))
    windows = _run(messages)
    assert [msg_id for w in windows for msg_id in w.msg_ids] == list(range(1, 200))
    by_id = {msg.msg_id: msg for msg in messages}
    for window in windows:
        assert 1 <= len(window.msg_ids) <= CFG.window_max_msgs
        lines = [units.render_line(by_id[msg_id]) for msg_id in window.msg_ids]
        assert window.text == "\n".join(lines)
        assert len("\n".join(lines[:-1])) < CFG.window_max_chars
        dates = [by_id[msg_id].date for msg_id in window.msg_ids]
        assert all(b - a <= 30 * 60 for a, b in zip(dates, dates[1:], strict=False))


# --- group_by_topic and build_unit -----------------------------------------------------------


def test_group_by_topic_non_forum() -> None:
    messages = [_msg(1), _msg(2, 1)]
    assert units.group_by_topic(messages) == {None: messages}


def test_group_by_topic_forum_keeps_first_seen_order() -> None:
    a1, b1, a2, g1 = (
        _msg(1, 0, topic_id=5),
        _msg(2, 1, topic_id=7),
        _msg(3, 2, topic_id=5),
        _msg(4, 3),
    )
    groups = units.group_by_topic([a1, b1, a2, g1])
    assert list(groups) == [5, 7, None]
    assert groups == {5: [a1, a2], 7: [b1], None: [g1]}


def test_group_by_topic_then_cut_isolates_gaps_per_topic() -> None:
    messages = [
        _msg(1, 0, topic_id=5),
        _msg(2, 20, topic_id=7),
        _msg(3, 40, topic_id=5),
        _msg(4, 60, topic_id=7),
    ]
    windows = [
        window
        for topic_id, group in units.group_by_topic(messages).items()
        for window in units.cut_windows(group, CFG, CHAT, topic_id)
    ]
    assert [(w.topic_id, w.msg_ids) for w in windows] == [(5, [1]), (5, [3]), (7, [2]), (7, [4])]


def test_chronological_sorts_by_date_then_msg_id() -> None:
    later, earlier, same_time = _msg(1, 5), _msg(2, 0), _msg(3, 5)
    assert units.chronological([later, same_time, earlier]) == [earlier, later, same_time]


def test_build_unit_renders_in_given_order() -> None:
    root, reply = _msg(9, 0, text="root"), _msg(4, 1, text="reply", reply_to_msg_id=9)
    unit = units.build_unit("thread", [root, reply], CHAT, None)
    assert unit.kind == "thread"
    assert unit.msg_ids == [9, 4]
    assert (unit.msg_id_start, unit.msg_id_end) == (4, 9)
    assert (unit.date_start, unit.date_end) == (BASE, BASE + 60)
    assert unit.text == "[2024-01-15 10:30] Alice: root\n[2024-01-15 10:31] Alice: reply"


def test_build_unit_rejects_empty() -> None:
    with pytest.raises(ValueError, match="at least one message"):
        units.build_unit("window", [], CHAT)
