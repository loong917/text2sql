"""Local model transport policy and ownership regressions."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from text2sql.core.config import load_settings
from text2sql.core.http_policy import ollama_http_options
from text2sql.infrastructure.ollama_generator import OllamaSqlGenerator
from text2sql.infrastructure.runtime import RuntimeResources
from text2sql.retrieval.table_retriever import OllamaEmbedder


@pytest.mark.parametrize(
    ("host", "trust_env"),
    [
        ("http://localhost:11434", False),
        ("localhost:11434", False),
        ("http://LOCALHOST.:11434", False),
        ("http://127.0.0.1:11434", False),
        ("http://127.0.0.2:11434", False),
        ("http://[::1]:11434", False),
        ("https://localhost:11434", True),
        ("http://localhost.example:11434", True),
        ("http://192.0.2.1:11434", True),
        ("https://model.example", True),
        ("http://[invalid", True),
    ],
)
def test_only_local_plain_http_bypasses_environment_proxy(host, trust_env):
    assert ollama_http_options(host) == {"trust_env": trust_env}


async def test_generator_and_retrieval_embedder_share_local_transport_policy():
    client = SimpleNamespace(_client=SimpleNamespace(aclose=AsyncMock()))
    with patch("ollama.AsyncClient", return_value=client) as factory:
        generator = OllamaSqlGenerator(
            host="http://localhost:11434",
            timeout_seconds=30,
            model="test",
            max_concurrency=1,
            num_ctx=1024,
            num_predict=256,
            keep_alive="1m",
            refusal_token="REFUSED",
        )
        embedder = OllamaEmbedder(
            host="http://localhost:11434",
            timeout_seconds=30,
            model="test",
            keep_alive="1m",
        )
    assert len(factory.call_args_list) == 2
    assert all(call.kwargs["trust_env"] is False for call in factory.call_args_list)
    await generator.aclose()
    await embedder.aclose()
    assert client._client.aclose.await_count == 2


def test_knowledge_embedding_replaces_and_closes_only_its_owned_transport():
    config = replace(load_settings(), llm_host="http://localhost:11434")
    runtime = RuntimeResources(config)
    previous = SimpleNamespace(_client=Mock())
    embedding = SimpleNamespace(_client=previous)
    replacement = Mock()
    with (
        patch(
            "text2sql.infrastructure.runtime.embedding_functions.OllamaEmbeddingFunction",
            return_value=embedding,
        ),
        patch("ollama.Client", return_value=replacement) as factory,
    ):
        assert runtime._embedding_function() is embedding
    previous._client.close.assert_called_once()
    assert embedding._client is replacement
    assert factory.call_args.kwargs["trust_env"] is False
    replacement._client.close()


def test_model_digest_probe_applies_policy_and_closes_client():
    config = replace(load_settings(), llm_host="http://127.0.0.1:11434")
    runtime = RuntimeResources(config)
    client = Mock()
    client.list.return_value = {"models": [{"name": "test", "digest": "sha"}]}
    with patch("ollama.Client", return_value=client) as factory:
        assert runtime.model_digests() == {"test": "sha"}
    assert factory.call_args.kwargs["trust_env"] is False
    client._client.close.assert_called_once()
