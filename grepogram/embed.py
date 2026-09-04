"""Dense embeddings: the :class:`Embedder` protocol, a deterministic fake and ``bge-m3``.

The dense side of search turns every unit and every query into one vector, and everything that
consumes those vectors (the ``unit_vec`` table, KNN, the hybrid ranking) only needs the three
things the protocol names: a ``name`` that identifies the embedding space (a vector from one
model is meaningless next to a vector from another, so ``meta.embed_model`` is compared against
it before anything is embedded), a ``dim`` the vector table is created for, and the two
``embed`` calls.

:class:`BgeM3Embedder` runs ``BAAI/bge-m3`` through sentence-transformers on the Mac GPU. The
import is lazy and every failure to import or load — the ``dense`` extra not installed, no
network for the first download, a broken cache — becomes :class:`ModelUnavailable`, so callers
degrade to lexical search with a warning instead of crashing; nothing in this module touches
torch at import time, which is what keeps the MCP server and the CLI usable without the extra.

:class:`FakeEmbedder` is what ``GREPOGRAM_FAKE_MODELS=1`` selects for tests and CI: a hashed
bag of stems with a tiny built-in Russian/English lexicon, so texts that share words (or a
lexicon pair such as ``ВНЖ`` / ``residence permit``) land close and everything else does not,
deterministically and without a model download.
"""

import hashlib
import logging
import math
import threading
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from grepogram.models import Config
from grepogram.paths import env_flag
from grepogram.stem import stem_token, tokenize

log = logging.getLogger(__name__)

FAKE_MODELS_ENV = "GREPOGRAM_FAKE_MODELS"
FAKE_NAME = "fake"
FAKE_DIM = 256
MAX_SEQ_LENGTH = 512
BATCH_SIZE = 32
AUTO_DEVICE = "auto"
FAKE_LEXICON: Mapping[str, str] = {
    "внж": "residence",
    "residence": "residence",
    "permit": "residence",
    "residencia": "residence",
    "банк": "bank",
    "bank": "bank",
    "счёт": "account",
    "account": "account",
    "виза": "visa",
    "visa": "visa",
    "симка": "sim",
    "сим": "sim",
    "sim": "sim",
}

_cpu_warned = False


class ModelUnavailable(Exception):
    """A local model cannot be imported or loaded; dense search degrades to lexical."""


@runtime_checkable
class Embedder(Protocol):
    """What the dense index and hybrid search need from an embedding model."""

    name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class FakeEmbedder:
    """Deterministic hashed bag-of-stems vectors for tests and CI.

    Every token of a text is stemmed like the lexical index stems it, mapped through the
    lexicon (so ``ВНЖ`` and ``residence permit`` become one feature), and hashed to a bucket
    and a sign; the vector is the signed feature counts, L2-normalized. Pure digits and
    one-letter tokens are skipped because every unit line starts with a ``[YYYY-MM-DD HH:MM]``
    stamp that would otherwise dominate the vector. An empty text is the zero vector.
    """

    name = FAKE_NAME

    def __init__(self, dim: int = FAKE_DIM, lexicon: Mapping[str, str] | None = None) -> None:
        if dim < 1:
            raise ValueError(f"embedding dimension must be positive, got {dim}")
        self.dim = dim
        source = FAKE_LEXICON if lexicon is None else lexicon
        self.lexicon = {stem_token(key.lower()): value for key, value in source.items()}

    def features(self, text: str) -> list[str]:
        """The hashed features of ``text``: stems mapped through the lexicon, stamps dropped."""
        out: list[str] = []
        for token in tokenize(text):
            if token.isdigit() or len(token) < 2:
                continue
            stem = stem_token(token)
            out.append(self.lexicon.get(stem, stem))
        return out

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for feature in self.features(text):
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            vector[int.from_bytes(digest[:4], "big") % self.dim] += 1.0 if digest[4] & 1 else -1.0
        return normalize(vector)


class BgeM3Embedder:
    """``BAAI/bge-m3`` (or any sentence-transformers model) on ``mps`` or the CPU.

    The model is loaded eagerly so that a missing extra, a failed download or a broken cache
    surfaces as :class:`ModelUnavailable` right here, where the caller can degrade. On ``mps``
    the weights are halved to fp16; ``max_seq_length`` is capped at 512 tokens, which covers a
    unit of ``window_max_chars`` characters with room to spare. Encoding is serialized with a
    lock because the MCP server may call it from several threads.
    """

    def __init__(self, model_id: str, device: str = AUTO_DEVICE) -> None:
        self.name = model_id
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise ModelUnavailable(
                f"sentence-transformers is not installed ({exc}); install the dense extra"
            ) from exc
        self.device = resolve_device(device)
        try:
            model = SentenceTransformer(model_id, device=self.device)
            if self.device == "mps":
                model.half()
        except Exception as exc:
            raise ModelUnavailable(f"cannot load embedding model {model_id!r}: {exc}") from exc
        model.max_seq_length = MAX_SEQ_LENGTH
        dim = embedding_dimension(model)
        if not dim:
            raise ModelUnavailable(f"embedding model {model_id!r} reports no embedding dimension")
        self.dim = dim
        self._model = model
        self._lock = threading.Lock()
        log.info("embedding model %s loaded on %s (dim %d)", model_id, self.device, dim)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        with self._lock:
            raw: Any = self._model.encode(
                texts,
                batch_size=BATCH_SIZE,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
        return as_rows(raw)

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text])[0]


def load_embedder(cfg: Config) -> Embedder:
    """The configured embedder, or the fake when ``GREPOGRAM_FAKE_MODELS`` is set.

    Raises :class:`ModelUnavailable` when the real model cannot be imported or loaded.
    """
    if fake_models_enabled():
        return FakeEmbedder()
    return BgeM3Embedder(cfg.models.embed, cfg.models.device)


def fake_models_enabled() -> bool:
    """True when ``GREPOGRAM_FAKE_MODELS`` is ``1``, ``true``, ``yes`` or ``on``."""
    return env_flag(FAKE_MODELS_ENV)


def resolve_device(device: str) -> str:
    """Turn the configured device into a torch device name.

    ``auto`` picks ``mps`` when Metal is available and falls back to the CPU with a warning
    logged once per process; any other value is passed through for torch to validate.
    """
    global _cpu_warned
    if device != AUTO_DEVICE:
        return device
    try:
        import torch
    except ImportError as exc:
        raise ModelUnavailable(f"torch is not installed ({exc}); install the dense extra") from exc
    if torch.backends.mps.is_available():
        return "mps"
    if not _cpu_warned:
        _cpu_warned = True
        log.warning("MPS is not available; models run on the CPU, which is much slower")
    return "cpu"


def normalize(vector: list[float]) -> list[float]:
    """L2-normalize ``vector``; the zero vector stays zero."""
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0.0:
        return vector
    return [x / norm for x in vector]


def embedding_dimension(model: Any) -> int | None:
    """The model's output width; sentence-transformers 6 renamed the getter, older ones lack it."""
    getter = getattr(model, "get_embedding_dimension", None)
    if getter is None:
        getter = model.get_sentence_embedding_dimension
    dim = getter()
    return int(dim) if dim else None


def as_rows(raw: Any) -> list[list[float]]:
    """Plain Python rows from whatever ``encode`` returned (an ndarray, a tensor or lists)."""
    rows = raw.tolist() if hasattr(raw, "tolist") else raw
    return [[float(value) for value in row] for row in rows]
