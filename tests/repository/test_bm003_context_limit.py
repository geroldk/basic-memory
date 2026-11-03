"""Protect the explicit Jina context limit and provider cache isolation."""

from types import SimpleNamespace

import fastembed
import pytest
from fastembed.common.types import NumpyArray
from fastembed.text.onnx_text_model import OnnxTextModel
from pydantic import ValidationError
from tokenizers import Tokenizer, models, pre_tokenizers, processors

from basic_memory.config import BasicMemoryConfig
from basic_memory.repository.embedding_provider_factory import (
    create_embedding_provider,
    reset_embedding_provider_cache,
)
from basic_memory.repository.fastembed_provider import FastEmbedEmbeddingProvider
from basic_memory.repository.semantic_vector_index_factory import semantic_embedding_identity

JINA = "jinaai/jina-embeddings-v2-base-de"
pytestmark = pytest.mark.real_embedder


def config(**overrides: object) -> BasicMemoryConfig:
    return BasicMemoryConfig(
        env="test",
        skip_initialization_sync=True,
        semantic_embedding_model=JINA,
        semantic_embedding_dimensions=768,
        **overrides,
    )


@pytest.fixture
def native(monkeypatch: pytest.MonkeyPatch) -> OnnxTextModel[NumpyArray]:
    tokenizer = Tokenizer(
        models.WordLevel(
            {"[UNK]": 0, "[CLS]": 1, "[SEP]": 2, "test": 3, "tail": 4},
            unk_token="[UNK]",
        )
    )
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer.post_processor = processors.TemplateProcessing(
        single="[CLS] $A [SEP]",
        special_tokens=[("[CLS]", 1), ("[SEP]", 2)],
    )
    tokenizer.enable_truncation(max_length=512, direction="left")
    instance = OnnxTextModel[NumpyArray]()
    instance.tokenizer = tokenizer
    monkeypatch.setattr(
        fastembed, "TextEmbedding", lambda **_kwargs: SimpleNamespace(model=instance)
    )
    return instance


def test_override_retains_tail_beyond_native_limit_and_preserves_direction(native) -> None:
    text = "test " * 600 + "tail"
    assert native.tokenizer.truncation["max_length"] == 512
    provider = FastEmbedEmbeddingProvider(model_name=JINA, dimensions=768, max_tokens=1024)
    model = provider._create_model()
    assert model.model is native
    encoded = native.tokenizer.encode(text)
    assert len(encoded.ids) == 603
    assert not encoded.overflowing
    assert native.tokenizer.truncation == {
        "max_length": 1024,
        "stride": 0,
        "strategy": "longest_first",
        "direction": "left",
    }
    assert provider.runtime_log_attrs()["max_tokens"] == 1024


def test_unset_override_retains_native_tokenizer(native) -> None:
    provider = FastEmbedEmbeddingProvider(model_name=JINA, dimensions=768)
    model = provider._create_model()
    assert model.model is native
    assert native.tokenizer.truncation["max_length"] == 512


def test_override_handles_tokenizer_without_serialized_truncation(native) -> None:
    native.tokenizer.no_truncation()
    provider = FastEmbedEmbeddingProvider(model_name=JINA, max_tokens=8192)
    assert provider._create_model().model is native
    assert native.tokenizer.truncation["direction"] == "right"


@pytest.mark.parametrize("value", [0, 1, 8193, True, 1024.5])
def test_direct_provider_rejects_invalid_limits(value) -> None:
    with pytest.raises(ValueError, match="integer between 2 and 8192"):
        FastEmbedEmbeddingProvider(model_name=JINA, max_tokens=value)


def test_direct_provider_rejects_unvalidated_model() -> None:
    with pytest.raises(ValueError, match="only for Jina-DE"):
        FastEmbedEmbeddingProvider(model_name="BAAI/bge-small-en-v1.5", max_tokens=8192)


def test_parallel_workers_cannot_silently_reload_native_limit() -> None:
    with pytest.raises(ValueError, match="single-process"):
        FastEmbedEmbeddingProvider(model_name=JINA, max_tokens=8192, parallel=2)


@pytest.mark.parametrize("missing", ["onnx", "tokenizer"])
def test_incompatible_runtime_fails_explicitly(monkeypatch, native, missing) -> None:
    if missing == "onnx":
        monkeypatch.setattr(
            fastembed, "TextEmbedding", lambda **_kwargs: SimpleNamespace(model=object())
        )
    else:
        native.tokenizer = None
    with pytest.raises(RuntimeError, match="initialized ONNX tokenizer"):
        FastEmbedEmbeddingProvider(model_name=JINA, max_tokens=8192)._create_model()


@pytest.mark.parametrize(
    "overrides",
    [
        {"semantic_embedding_max_tokens": 0},
        {"semantic_embedding_max_tokens": 8193},
        {"semantic_embedding_max_tokens": True},
        {"semantic_embedding_max_tokens": 8192, "semantic_embedding_provider": "openai"},
        {"semantic_embedding_max_tokens": 8192, "semantic_embedding_model": "bge-small-en-v1.5"},
        {"semantic_embedding_max_tokens": 8192, "semantic_embedding_parallel": 2},
    ],
)
def test_config_rejects_unsupported_overrides(overrides) -> None:
    values = {"env": "test", "skip_initialization_sync": True, "semantic_embedding_model": JINA}
    with pytest.raises(ValidationError):
        BasicMemoryConfig(**(values | overrides))


def test_config_roundtrip_and_environment_override(monkeypatch) -> None:
    explicit = config(semantic_embedding_max_tokens=8192, semantic_embedding_parallel=1)
    assert (
        BasicMemoryConfig.model_validate_json(
            explicit.model_dump_json()
        ).semantic_embedding_max_tokens
        == 8192
    )
    monkeypatch.setenv("BASIC_MEMORY_SEMANTIC_EMBEDDING_MAX_TOKENS", "2048")
    assert config().semantic_embedding_max_tokens == 2048


def test_factory_separates_limits_but_keeps_model_identity() -> None:
    reset_embedding_provider_cache()
    try:
        first = create_embedding_provider(config(semantic_embedding_max_tokens=512))
        second = create_embedding_provider(config(semantic_embedding_max_tokens=8192))
        assert isinstance(first, FastEmbedEmbeddingProvider)
        assert isinstance(second, FastEmbedEmbeddingProvider)
        assert first is not second
        assert second is create_embedding_provider(config(semantic_embedding_max_tokens=8192))
        assert first.max_tokens == 512 and second.max_tokens == 8192
        # The rollout separately proves existing chunks fit both budgets and that
        # their model vectors are unchanged; a new ceiling does not replace weights.
        assert semantic_embedding_identity(first) == semantic_embedding_identity(second)
    finally:
        reset_embedding_provider_cache()
