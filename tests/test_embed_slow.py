"""Sanity checks against the real ``BAAI/bge-m3``.

Run with ``uv run pytest -m slow tests/test_embed_slow.py``; the first run downloads the model
(about 2.2 GB) into the Hugging Face cache. Skipped when the ``dense`` extra is not installed.
"""

import math
import time
from collections.abc import Iterator

import pytest

from grepogram.embed import BgeM3Embedder
from grepogram.models import ModelsCfg

pytestmark = pytest.mark.slow

RU = "Как открыть счёт в банке без ВНЖ, только с паспортом?"
EN = "How can I open a bank account without a residence permit, using just a passport?"
UNRELATED = "Завтра обещают сильный дождь, не забудь взять зонт."
PARAPHRASE_MIN = 0.6
GAP_MIN = 0.15
UNIT_LINES = [
    "[2024-03-15 10:30] Alice: Открыл счёт в Galicia без DNI, только с паспортом и CUIT",
    "[2024-03-15 10:41] Bob: Which branch? They asked me for a residence certificate",
    "[2024-03-15 10:45] Alice: Microcentro, менеджер Хуан, без записи",
    "[2024-03-15 11:02] Ольга: А карту сразу выдали или ждать?",
    "[2024-03-15 11:05] Alice: Дебетовую сразу, кредитную после трёх месяцев",
]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def length(vector: list[float]) -> float:
    return math.sqrt(sum(x * x for x in vector))


@pytest.fixture(scope="module")
def embedder() -> Iterator[BgeM3Embedder]:
    pytest.importorskip("sentence_transformers")
    cfg = ModelsCfg()
    yield BgeM3Embedder(cfg.embed, cfg.device)


def test_reports_model_and_dimension(embedder: BgeM3Embedder) -> None:
    assert embedder.name == "BAAI/bge-m3"
    assert embedder.dim == 1024
    assert embedder.device in {"mps", "cpu"}


def test_vectors_are_finite_unit_length(embedder: BgeM3Embedder) -> None:
    vectors = embedder.embed([RU, EN, UNRELATED, ""])
    assert [len(vector) for vector in vectors] == [1024] * 4
    for vector in vectors:
        assert all(math.isfinite(value) for value in vector)
        assert length(vector) == pytest.approx(1.0, abs=2e-2)


def test_paraphrase_closer_than_unrelated(embedder: BgeM3Embedder) -> None:
    ru, en, other = embedder.embed([RU, EN, UNRELATED])
    paraphrase = cosine(ru, en)
    assert paraphrase > PARAPHRASE_MIN
    assert paraphrase > cosine(ru, other) + GAP_MIN
    assert paraphrase > cosine(en, other) + GAP_MIN


def test_query_embedding_matches_batch_row(embedder: BgeM3Embedder) -> None:
    single = embedder.embed_query(RU)
    batched = embedder.embed([RU, EN, UNRELATED])[0]
    assert cosine(single, batched) > 0.995


def test_unit_sized_batch_throughput(embedder: BgeM3Embedder) -> None:
    unit = "\n".join(UNIT_LINES)
    texts = [f"{unit}\n[2024-03-15 11:{10 + i:02d}] Bob: message {i}" for i in range(64)]
    embedder.embed(texts[:4])
    started = time.perf_counter()
    vectors = embedder.embed(texts)
    elapsed = time.perf_counter() - started
    assert len(vectors) == 64
    print(f"\nbge-m3 on {embedder.device}: {64 / elapsed:.1f} units/s ({elapsed:.1f} s for 64)")
