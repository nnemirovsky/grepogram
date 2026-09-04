import logging
import sys
import threading
import types
from collections.abc import Callable
from typing import Any

import pytest

from grepogram import embed
from grepogram.embed import ModelUnavailable
from grepogram.models import Config, ModelsCfg
from grepogram.rerank import (
    BATCH_SIZE,
    MAX_SEQ_LENGTH,
    BgeReranker,
    FakeReranker,
    Reranker,
    as_scores,
    load_reranker,
)

QUERY_RU = "Как открыть счёт в банке без DNI?"
RU_BANK = "Открыл счёт в банке Galicia без DNI, только с паспортом"
RU_BANK_INFLECTED = "открываю счета в банках без dni по паспорту"
RU_SIM = "Симку Claro купил без DNI в киоске, паспорт не спросили"
WEATHER = "Завтра обещают дождь, возьми зонт"


# --- FakeReranker ----------------------------------------------------------------------------


def test_fake_satisfies_protocol() -> None:
    fake = FakeReranker()
    assert isinstance(fake, Reranker)
    assert fake.name == "fake"


def test_fake_orders_relevant_text_above_distractors() -> None:
    scores = FakeReranker().score(QUERY_RU, [WEATHER, RU_BANK, RU_SIM])
    assert len(scores) == 3
    assert scores[1] > scores[2] > scores[0]
    assert scores[0] == 0.0


def test_fake_scores_are_the_share_of_query_features_found() -> None:
    fake = FakeReranker()
    assert fake.features(QUERY_RU) == ["как", "откр", "account", "bank", "без", "dni"]
    assert fake.score(QUERY_RU, [QUERY_RU]) == [1.0]
    assert fake.score("DNI паспорт", [RU_BANK, "паспорт", WEATHER]) == [1.0, 0.5, 0.0]


def test_fake_scores_stay_within_the_unit_interval() -> None:
    for score in FakeReranker().score(QUERY_RU, [RU_BANK, RU_BANK * 3, WEATHER, ""]):
        assert 0.0 <= score <= 1.0


def test_fake_counts_inflections_through_stems() -> None:
    scores = FakeReranker().score(QUERY_RU, [RU_BANK_INFLECTED, WEATHER])
    assert scores[0] > 0.5
    assert scores[1] == 0.0


def test_fake_lexicon_bridges_the_paraphrase_pair() -> None:
    fake = FakeReranker()
    assert fake.score("ВНЖ", ["нужен residence permit", WEATHER]) == [1.0, 0.0]
    assert fake.score("residence permit", ["получил ВНЖ"]) == [1.0]


def test_fake_custom_lexicon_replaces_the_default() -> None:
    fake = FakeReranker(lexicon={"Кот": "cat"})
    assert fake.score("кот", ["cats"]) == [1.0]
    assert fake.score("ВНЖ", ["residence permit"]) == [0.0]


@pytest.mark.parametrize("query", ["", "   ", "🙂🙂 !!!", "2024 03 15", "a в i"])
def test_fake_query_without_features_scores_zero(query: str) -> None:
    assert FakeReranker().score(query, [RU_BANK, WEATHER]) == [0.0, 0.0]


def test_fake_ignores_stamps_and_repeats() -> None:
    fake = FakeReranker()
    line = "[2024-03-15 10:30] Alice: " + RU_BANK
    assert fake.score(QUERY_RU, [line]) == fake.score(QUERY_RU, [RU_BANK])
    assert fake.score("2024-03-15 10:30", [line]) == [0.0]
    assert fake.score(QUERY_RU, [RU_BANK + " " + RU_BANK]) == fake.score(QUERY_RU, [RU_BANK])


def test_fake_empty_texts() -> None:
    assert FakeReranker().score(QUERY_RU, []) == []


def test_fake_is_deterministic_across_instances() -> None:
    texts = [RU_BANK, RU_SIM, WEATHER, ""]
    assert FakeReranker().score(QUERY_RU, texts) == FakeReranker().score(QUERY_RU, texts)


def test_fake_is_usable_from_a_worker_thread() -> None:
    fake = FakeReranker()
    results: list[list[float]] = []
    worker = threading.Thread(target=lambda: results.append(fake.score(QUERY_RU, [RU_BANK])))
    worker.start()
    worker.join()
    assert results == [fake.score(QUERY_RU, [RU_BANK])]


# --- helpers ---------------------------------------------------------------------------------


class _ArrayLike:
    def __init__(self, values: Any) -> None:
        self.values = values

    def tolist(self) -> Any:
        return self.values


def test_as_scores_accepts_arrays_scalars_and_lists() -> None:
    assert as_scores(_ArrayLike([1, 0.5])) == [1.0, 0.5]
    assert as_scores(_ArrayLike(0.25)) == [0.25]
    assert as_scores([0.5, 1]) == [0.5, 1.0]
    assert as_scores([]) == []
    assert all(isinstance(value, float) for value in as_scores(_ArrayLike([1, 2])))


# --- BgeReranker through stub modules ----------------------------------------------------------


class StubCrossEncoder:
    """Stands in for ``sentence_transformers.CrossEncoder``; scores a pair by its text length."""

    instances: list["StubCrossEncoder"] = []

    def __init__(self, model_name_or_path: str, device: str | None = None, **kwargs: Any) -> None:
        self.model_id = model_name_or_path
        self.device = device
        self.kwargs = kwargs
        self.halved = False
        self.calls: list[tuple[list[tuple[str, str]], dict[str, Any]]] = []
        StubCrossEncoder.instances.append(self)

    def half(self) -> "StubCrossEncoder":
        self.halved = True
        return self

    def predict(self, pairs: list[tuple[str, str]], **kwargs: Any) -> _ArrayLike:
        self.calls.append((pairs, kwargs))
        return _ArrayLike([len(text) for _, text in pairs])


class OfflineCrossEncoder(StubCrossEncoder):
    def __init__(self, model_name_or_path: str, device: str | None = None, **kwargs: Any) -> None:
        raise OSError("offline: cannot reach huggingface.co")


class LocalEntryNotFoundError(FileNotFoundError):
    """huggingface_hub's "not in the cache" error under its own name — see the twin in
    ``tests/test_embed.py``; the name is all :func:`grepogram.embed.not_cached` matches on."""


class UncachedCrossEncoder(StubCrossEncoder):
    """A cross-encoder the cache does not hold: the ``local_files_only`` load fails the way
    transformers reports it and the download that follows succeeds. ``attempts`` records the
    ``local_files_only`` of every construction, failed ones included."""

    attempts: list[bool] = []

    def __init__(self, model_name_or_path: str, device: str | None = None, **kwargs: Any) -> None:
        local = bool(kwargs.get("local_files_only"))
        UncachedCrossEncoder.attempts.append(local)
        if local:
            try:
                raise LocalEntryNotFoundError("cannot find the requested files in the disk cache")
            except LocalEntryNotFoundError as exc:
                raise OSError("We couldn't connect to 'https://huggingface.co'") from exc
        super().__init__(model_name_or_path, device, **kwargs)


class ShortCrossEncoder(StubCrossEncoder):
    def predict(self, pairs: list[tuple[str, str]], **kwargs: Any) -> _ArrayLike:
        return _ArrayLike([1.0])


Install = Callable[..., None]


@pytest.fixture
def stubs(monkeypatch: pytest.MonkeyPatch) -> Install:
    """Inject stub ``torch`` / ``sentence_transformers`` modules; the real ones are never used."""

    def install(
        *,
        mps: bool = True,
        model: type[StubCrossEncoder] | None = StubCrossEncoder,
        torch_missing: bool = False,
    ) -> None:
        torch_mod: Any = types.ModuleType("torch")
        torch_mod.backends = types.SimpleNamespace(
            mps=types.SimpleNamespace(is_available=lambda: mps)
        )
        monkeypatch.setitem(sys.modules, "torch", None if torch_missing else torch_mod)
        st_mod: Any = types.ModuleType("sentence_transformers")
        st_mod.CrossEncoder = model
        monkeypatch.setitem(sys.modules, "sentence_transformers", None if model is None else st_mod)
        monkeypatch.setattr(embed, "_cpu_warned", False)
        monkeypatch.delenv("GREPOGRAM_FAKE_MODELS", raising=False)
        # CI runs the whole suite with HF_HUB_OFFLINE=1; these tests decide it themselves
        monkeypatch.delenv(embed.HF_OFFLINE_ENV, raising=False)
        StubCrossEncoder.instances.clear()
        UncachedCrossEncoder.attempts.clear()

    return install


def test_bge_loads_on_mps_in_fp16_with_max_length(stubs: Install) -> None:
    stubs(mps=True)
    reranker = BgeReranker("BAAI/bge-reranker-v2-m3", "auto")
    (model,) = StubCrossEncoder.instances
    assert isinstance(reranker, Reranker)
    assert reranker.name == "BAAI/bge-reranker-v2-m3"
    assert reranker.device == "mps"
    assert model.model_id == "BAAI/bge-reranker-v2-m3"
    assert model.device == "mps"
    assert model.kwargs == {"max_length": MAX_SEQ_LENGTH, "local_files_only": True}
    assert MAX_SEQ_LENGTH == 512
    assert model.halved is True


def test_bge_on_cpu_keeps_fp32(stubs: Install) -> None:
    stubs(mps=False)
    reranker = BgeReranker("BAAI/bge-reranker-v2-m3", "cpu")
    (model,) = StubCrossEncoder.instances
    assert reranker.device == "cpu"
    assert model.device == "cpu"
    assert model.halved is False


@pytest.mark.parametrize("device", ["cpu", "mps", "cuda:1"])
def test_bge_passes_explicit_devices_through_without_torch(stubs: Install, device: str) -> None:
    stubs(torch_missing=True)
    assert BgeReranker("BAAI/bge-reranker-v2-m3", device).device == device


def test_bge_auto_device_falls_back_to_cpu_with_one_warning(
    stubs: Install, caplog: pytest.LogCaptureFixture
) -> None:
    stubs(mps=False)
    with caplog.at_level(logging.WARNING, logger="grepogram.embed"):
        assert BgeReranker("BAAI/bge-reranker-v2-m3").device == "cpu"
        assert BgeReranker("BAAI/bge-reranker-v2-m3").device == "cpu"
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "MPS" in warnings[0].getMessage()


def test_bge_auto_device_without_torch(stubs: Install) -> None:
    stubs(torch_missing=True)
    with pytest.raises(ModelUnavailable, match="torch"):
        BgeReranker("BAAI/bge-reranker-v2-m3")


def test_bge_scores_pairs_in_batches_without_progress_bar(stubs: Install) -> None:
    stubs()
    reranker = BgeReranker("BAAI/bge-reranker-v2-m3")
    scores = reranker.score("q", ["a", "bbb"])
    assert scores == [1.0, 3.0]
    assert all(isinstance(value, float) for value in scores)
    (model,) = StubCrossEncoder.instances
    assert model.calls == [
        (
            [("q", "a"), ("q", "bbb")],
            {"batch_size": BATCH_SIZE, "show_progress_bar": False, "convert_to_numpy": True},
        )
    ]
    assert BATCH_SIZE == 32


def test_bge_single_text_is_still_a_list(stubs: Install) -> None:
    stubs()
    assert BgeReranker("BAAI/bge-reranker-v2-m3").score("q", ["hello"]) == [5.0]


def test_bge_empty_texts_skip_the_model(stubs: Install) -> None:
    stubs()
    reranker = BgeReranker("BAAI/bge-reranker-v2-m3")
    assert reranker.score("q", []) == []
    assert StubCrossEncoder.instances[0].calls == []


def test_bge_score_from_a_worker_thread(stubs: Install) -> None:
    stubs()
    reranker = BgeReranker("BAAI/bge-reranker-v2-m3")
    results: list[list[float]] = []
    worker = threading.Thread(target=lambda: results.append(reranker.score("q", ["xy"])))
    worker.start()
    worker.join()
    assert results == [[2.0]]


def test_bge_without_sentence_transformers(stubs: Install) -> None:
    stubs(model=None)
    with pytest.raises(ModelUnavailable, match="sentence-transformers is not installed"):
        BgeReranker("BAAI/bge-reranker-v2-m3")


def test_bge_download_failure_becomes_model_unavailable(stubs: Install) -> None:
    stubs(model=OfflineCrossEncoder)
    with pytest.raises(ModelUnavailable, match="'BAAI/bge-reranker-v2-m3'.*offline") as info:
        BgeReranker("BAAI/bge-reranker-v2-m3")
    assert isinstance(info.value.__cause__, OSError)


def test_bge_rejects_a_short_answer(stubs: Install) -> None:
    stubs(model=ShortCrossEncoder)
    reranker = BgeReranker("BAAI/bge-reranker-v2-m3")
    with pytest.raises(RuntimeError, match="1 scores for 2 texts"):
        reranker.score("q", ["a", "b"])


# --- loading from the cache ------------------------------------------------------------------


def test_bge_asks_for_the_cached_files_only(
    stubs: Install, caplog: pytest.LogCaptureFixture
) -> None:
    """The cross-encoder is loaded from the cache without a hub round trip, like the embedder;
    a search that reranks would otherwise pay it twice."""
    stubs()
    with caplog.at_level(logging.INFO, logger="grepogram.embed"):
        BgeReranker("BAAI/bge-reranker-v2-m3", "cpu")
    (model,) = StubCrossEncoder.instances
    assert model.kwargs["local_files_only"] is True
    assert UncachedCrossEncoder.attempts == []
    assert [r for r in caplog.records if "downloading" in r.getMessage()] == []


def test_bge_downloads_once_when_the_cache_holds_nothing(
    stubs: Install, caplog: pytest.LogCaptureFixture
) -> None:
    stubs(model=UncachedCrossEncoder)
    with caplog.at_level(logging.INFO, logger="grepogram.embed"):
        BgeReranker("BAAI/bge-reranker-v2-m3", "cpu")
    assert UncachedCrossEncoder.attempts == [True, False]
    (model,) = StubCrossEncoder.instances
    assert model.kwargs["local_files_only"] is False
    downloads = [r for r in caplog.records if "downloading" in r.getMessage()]
    assert len(downloads) == 1
    assert "reranker model 'BAAI/bge-reranker-v2-m3'" in downloads[0].getMessage()


def test_bge_never_retries_with_the_network_under_hf_hub_offline(
    stubs: Install, monkeypatch: pytest.MonkeyPatch
) -> None:
    stubs(model=UncachedCrossEncoder)
    monkeypatch.setenv(embed.HF_OFFLINE_ENV, "1")
    with pytest.raises(ModelUnavailable, match="cannot load reranker model"):
        BgeReranker("BAAI/bge-reranker-v2-m3", "cpu")
    assert UncachedCrossEncoder.attempts == [True]
    assert StubCrossEncoder.instances == []


# --- load_reranker ---------------------------------------------------------------------------


def test_load_reranker_returns_the_fake_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GREPOGRAM_FAKE_MODELS", "1")
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    assert isinstance(load_reranker(Config()), FakeReranker)


def test_load_reranker_uses_the_configured_model_and_device(stubs: Install) -> None:
    stubs(mps=True)
    cfg = Config(models=ModelsCfg(rerank="BAAI/bge-reranker-base", device="cpu"))
    reranker = load_reranker(cfg)
    assert isinstance(reranker, BgeReranker)
    assert reranker.name == "BAAI/bge-reranker-base"
    assert reranker.device == "cpu"
    assert StubCrossEncoder.instances[0].model_id == "BAAI/bge-reranker-base"


def test_load_reranker_resolves_auto_device(stubs: Install) -> None:
    stubs(mps=True)
    reranker = load_reranker(Config())
    assert isinstance(reranker, BgeReranker)
    assert reranker.name == "BAAI/bge-reranker-v2-m3"
    assert reranker.device == "mps"


def test_load_reranker_raises_when_the_import_fails(stubs: Install) -> None:
    stubs(model=None)
    with pytest.raises(ModelUnavailable):
        load_reranker(Config())


def test_tmp_home_selects_the_fake(tmp_home: object) -> None:
    assert isinstance(load_reranker(Config()), FakeReranker)
