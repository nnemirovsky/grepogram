"""Cross-encoder reranking: the :class:`Reranker` protocol, a deterministic fake and ``bge``.

Hybrid search fuses the lexical and dense rankings and then hands the top ``rerank_top`` units
to a cross-encoder, which reads the query and each unit together and scores how well they
match; the fused list is re-sorted by those scores. Everything downstream needs one call from
the model, ``score(query, texts)``, and that is the whole protocol.

:class:`BgeReranker` runs ``BAAI/bge-reranker-v2-m3`` through sentence-transformers'
``CrossEncoder`` on the Mac GPU with the same device selection as the embedder, and through the
same :func:`~grepogram.embed.load_cached_first` that keeps a cached model off the network. The
import is lazy and every failure to import or load — the ``dense`` extra not installed, no
network for the first download, a broken cache — becomes :class:`ModelUnavailable`, so callers
skip the rerank step with a warning instead of crashing; nothing here touches torch at import
time.

:class:`FakeReranker` is what ``GREPOGRAM_FAKE_MODELS=1`` selects for tests and CI: the share
of the query's stems found in each text, through the same lexicon as :class:`FakeEmbedder`, so
the fake stack agrees with itself about ``ВНЖ`` and ``residence permit``.
"""

import logging
import threading
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from grepogram.embed import (
    AUTO_DEVICE,
    DEFAULT_MAX_SEQ_LENGTH,
    FakeEmbedder,
    ModelUnavailable,
    fake_models_enabled,
    load_cached_first,
    resolve_device,
)
from grepogram.models import Config

log = logging.getLogger(__name__)

FAKE_NAME = "fake"
BATCH_SIZE = 32


@runtime_checkable
class Reranker(Protocol):
    """What hybrid search needs from a cross-encoder: one relevance score per text."""

    name: str

    def score(self, query: str, texts: list[str]) -> list[float]: ...


class FakeReranker:
    """Deterministic query-term overlap for tests and CI.

    Each score is the fraction of the query's features (stems through the fake embedder's
    lexicon, stamps and digits dropped) that occur in the text, so a text carrying every query
    term scores 1.0, one carrying none 0.0, and inflections or a lexicon pair still count. A
    query without features scores every text 0.0.
    """

    name = FAKE_NAME

    def __init__(self, lexicon: Mapping[str, str] | None = None) -> None:
        self._embedder = FakeEmbedder(lexicon=lexicon)

    def features(self, text: str) -> list[str]:
        """The features compared for overlap: the fake embedder's stems for ``text``."""
        return self._embedder.features(text)

    def score(self, query: str, texts: list[str]) -> list[float]:
        wanted = set(self.features(query))
        if not wanted:
            return [0.0 for _ in texts]
        return [len(wanted & set(self.features(text))) / len(wanted) for text in texts]


class BgeReranker:
    """``BAAI/bge-reranker-v2-m3`` (or any sentence-transformers cross-encoder) on ``mps`` or CPU.

    The model is loaded eagerly so that a missing extra, a failed download or a broken cache
    surfaces as :class:`ModelUnavailable` right here, where the caller can degrade. On ``mps``
    the weights are halved to fp16; the query and the unit are truncated together at
    ``max_seq_length`` tokens — the same ``models.max_seq_length`` the embedder reads, because
    both read the *same* unit text and a cap that let one of them see more of a unit than the
    other would have the two stages rank different documents. The query shares that budget, so
    the reranker sees marginally less of a long unit than the embedder does. Scoring is
    serialized with a lock because the MCP server may call it from several threads.
    """

    def __init__(
        self,
        model_id: str,
        device: str = AUTO_DEVICE,
        max_seq_length: int = DEFAULT_MAX_SEQ_LENGTH,
    ) -> None:
        if max_seq_length < 1:
            raise ValueError(f"max_seq_length must be positive, got {max_seq_length}")
        self.name = model_id
        self.max_seq_length = max_seq_length
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as exc:
            raise ModelUnavailable(
                f"sentence-transformers is not installed ({exc}); install the dense extra"
            ) from exc
        self.device = resolve_device(device)
        try:
            model = load_cached_first(
                lambda local: CrossEncoder(
                    model_id,
                    device=self.device,
                    max_length=max_seq_length,
                    local_files_only=local,
                ),
                f"reranker model {model_id!r}",
            )
            if self.device == "mps":
                model.half()
        except Exception as exc:
            raise ModelUnavailable(f"cannot load reranker model {model_id!r}: {exc}") from exc
        self._model = model
        self._lock = threading.Lock()
        log.info(
            "reranker model %s loaded on %s (%d tokens)", model_id, self.device, max_seq_length
        )

    def score(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        pairs = [(query, text) for text in texts]
        with self._lock:
            raw: Any = self._model.predict(
                pairs,
                batch_size=BATCH_SIZE,
                show_progress_bar=False,
                convert_to_numpy=True,
            )
        scores = as_scores(raw)
        if len(scores) != len(texts):
            raise RuntimeError(
                f"reranker model {self.name!r} returned {len(scores)} scores for {len(texts)} texts"
            )
        return scores


def load_reranker(cfg: Config) -> Reranker:
    """The configured reranker, or the fake when ``GREPOGRAM_FAKE_MODELS`` is set.

    Raises :class:`ModelUnavailable` when the real model cannot be imported or loaded.
    """
    if fake_models_enabled():
        return FakeReranker()
    return BgeReranker(cfg.models.rerank, cfg.models.device, cfg.models.max_seq_length)


def as_scores(raw: Any) -> list[float]:
    """Plain Python floats from whatever ``predict`` returned (an ndarray, a tensor or a list)."""
    values = raw.tolist() if hasattr(raw, "tolist") else raw
    if isinstance(values, int | float):
        return [float(values)]
    return [float(value) for value in values]
