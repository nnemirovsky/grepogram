"""Sanity checks against the real ``BAAI/bge-reranker-v2-m3``.

Run with ``uv run pytest -m slow tests/test_rerank_slow.py``; the first run downloads the model
(about 2.2 GB) into the Hugging Face cache. Skipped when the ``dense`` extra is not installed.
"""

import math
import time
from collections.abc import Iterator

import pytest

from grepogram.models import ModelsCfg
from grepogram.rerank import BgeReranker

pytestmark = pytest.mark.slow

QUERY_RU = "Как открыть счёт в банке без ВНЖ, только с паспортом?"
QUERY_EN = "How can I open a bank account without a residence permit, using just a passport?"
RELEVANT = "\n".join(
    [
        "[2024-03-15 10:30] Ольга: Подскажите, где открыть счёт без DNI? Только приехала.",
        "[2024-03-15 10:32] Bob: Без DNI почти нигде. Сначала CUIT, потом Galicia или Santander.",
        "[2024-03-15 10:33] Alice: Brubank открыл мне счёт по паспорту, без DNI, лимиты маленькие.",
        "[2024-03-15 10:35] Ольга: Спасибо, попробую Brubank.",
    ]
)
DISTRACTOR = "\n".join(
    [
        "[2024-03-15 12:10] Дима: Симку Claro купил в киоске, паспорт не спросили.",
        "[2024-03-15 12:12] Maria: Personal has better coverage outside the city.",
        "[2024-03-15 12:15] Дима: Пополнял через Mercado Pago, всё сразу работает.",
    ]
)
UNRELATED = "\n".join(
    [
        "[2024-03-16 09:00] Alice: Завтра обещают сильный дождь, не забудьте зонт.",
        "[2024-03-16 09:05] Bob: В воскресенье парк закрыт, идём в субботу.",
    ]
)
GAP_MIN = 0.1


@pytest.fixture(scope="module")
def reranker() -> Iterator[BgeReranker]:
    pytest.importorskip("sentence_transformers")
    cfg = ModelsCfg()
    yield BgeReranker(cfg.rerank, cfg.device)


def test_reports_model_and_device(reranker: BgeReranker) -> None:
    assert reranker.name == "BAAI/bge-reranker-v2-m3"
    assert reranker.device in {"mps", "cpu"}


def test_scores_are_finite_floats_per_text(reranker: BgeReranker) -> None:
    scores = reranker.score(QUERY_RU, [RELEVANT, DISTRACTOR, UNRELATED, ""])
    assert len(scores) == 4
    assert all(isinstance(score, float) and math.isfinite(score) for score in scores)
    assert len(set(scores)) > 1


def test_relevant_unit_ranks_above_the_distractor(reranker: BgeReranker) -> None:
    relevant, distractor, unrelated = reranker.score(QUERY_RU, [RELEVANT, DISTRACTOR, UNRELATED])
    assert relevant > distractor + GAP_MIN
    assert relevant > unrelated + GAP_MIN


def test_english_query_ranks_the_russian_unit_first(reranker: BgeReranker) -> None:
    relevant, distractor, unrelated = reranker.score(QUERY_EN, [RELEVANT, DISTRACTOR, UNRELATED])
    assert relevant > distractor + GAP_MIN
    assert relevant > unrelated + GAP_MIN


def test_single_text_matches_its_batch_score(reranker: BgeReranker) -> None:
    (single,) = reranker.score(QUERY_RU, [RELEVANT])
    batched = reranker.score(QUERY_RU, [RELEVANT, DISTRACTOR, UNRELATED])[0]
    assert single == pytest.approx(batched, abs=2e-2)


def test_rerank_top_sized_batch_throughput(reranker: BgeReranker) -> None:
    texts = [f"{RELEVANT}\n[2024-03-15 10:{40 + i:02d}] Bob: message {i}" for i in range(40)]
    reranker.score(QUERY_RU, texts[:4])
    started = time.perf_counter()
    scores = reranker.score(QUERY_RU, texts)
    elapsed = time.perf_counter() - started
    assert len(scores) == 40
    rate = 40 / elapsed
    print(f"\nbge-reranker on {reranker.device}: {rate:.1f} pairs/s ({elapsed:.1f} s for 40)")
