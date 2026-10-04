"""Pure Chroma evidence contracts using the pinned Vanna text-memory layout."""

import unittest
from copy import deepcopy
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import chromadb
import numpy as np
from chromadb.config import Settings as ChromaSettings
from chromadb.errors import NotFoundError
from chromadb.utils.embedding_functions import OllamaEmbeddingFunction

from text2sql.core.config import load_settings
from text2sql.infrastructure.runtime import ChromaAgentMemory, RuntimeResources
from text2sql.knowledge.collection_evidence import (
    assert_collection_evidence,
    normalize_collection_configuration,
    validate_collection_evidence,
)
from text2sql.knowledge.collection_evidence import (
    build_collection_evidence as _build_collection_evidence,
)


def collection_configuration(embedding_config=None):
    return {
        "hnsw": {"space": "cosine", "ef_search": 100},
        "spann": None,
        "embedding_function": {
            "type": "known",
            "name": "ollama",
            "config": embedding_config
            or {
                "url": "http://localhost:11434/api/embeddings",
                "model_name": "nomic-embed-text",
                "timeout": 120,
            },
        },
    }


def build_collection_evidence(payload, *, configuration=None, collection_metadata=None, **kwargs):
    return _build_collection_evidence(
        payload,
        configuration=configuration if configuration is not None else collection_configuration(),
        collection_metadata=collection_metadata,
        **kwargs,
    )


def stored_collection(runtime, *, payload=None, count=2):
    collection = Mock()
    collection.configuration_json = collection_configuration(runtime._embedding_config())
    collection.metadata = None
    collection.get.return_value = payload if payload is not None else collection_payload()
    collection.count.return_value = count
    return collection


def collection_payload():
    return {
        "ids": ["b", "a"],
        "documents": ["metric", "policy"],
        "metadatas": [
            {"content": text, "timestamp": "2026-10-02T10:00:00", "is_text_memory": True}
            for text in ("metric", "policy")
        ],
        "embeddings": np.array([[1.0, 2.0], [2.0, 1.0]], dtype=np.float32),
    }


def index_records():
    return [
        {"content": "metric", "source_type": "metric_rule"},
        {"content": "policy", "source_type": "domain_policy"},
        {"content": "schema", "source_type": "column_schema"},
    ]


class CollectionEvidenceTests(unittest.TestCase):
    def test_order_and_numpy_representation_do_not_change_identity(self):
        payload = collection_payload()
        expected = build_collection_evidence(payload, index_records=index_records())
        reordered = {name: list(reversed(list(value))) for name, value in payload.items()}
        self.assertEqual(
            build_collection_evidence(reordered, index_records=index_records()), expected
        )
        self.assertEqual(expected["count"], 2)
        self.assertEqual(expected["embedding_dimensions"], 2)
        self.assertEqual(validate_collection_evidence(expected), [])

    def test_id_document_metadata_and_embedding_are_each_content_addressed(self):
        expected = build_collection_evidence(collection_payload())
        for field in ("ids", "documents", "metadatas", "embeddings"):
            payload = collection_payload()
            if field == "ids":
                payload[field][0] = "different-id"
            elif field == "documents":
                payload[field][0] = "other text"
                payload["metadatas"][0]["content"] = "other text"
            elif field == "metadatas":
                payload[field][0]["timestamp"] = "2026-10-03T10:00:00"
            else:
                payload[field][0][0] = 3.0
            with self.subTest(field=field):
                self.assertNotEqual(
                    build_collection_evidence(payload)["sha256"], expected["sha256"]
                )

    def test_index_registration_cannot_hide_missing_or_additional_documents(self):
        for records in (
            index_records()[:1],
            index_records() + [{"content": "missing", "source_type": "metric_rule"}],
        ):
            with (
                self.subTest(records=records),
                self.assertRaisesRegex(ValueError, "registered index"),
            ):
                build_collection_evidence(collection_payload(), index_records=records)
        payload = collection_payload()
        for field in payload:
            payload[field] = list(payload[field]) + [deepcopy(payload[field][0])]
        payload["ids"][-1] = "duplicate-text-other-id"
        self.assertEqual(
            build_collection_evidence(payload, index_records=index_records())["count"], 3
        )

    def test_bad_record_types_layout_vectors_and_array_lengths_fail_closed(self):
        bad = []
        for value in (
            None,
            [],
            [[0.0, 0.0], [1.0, 1.0]],
            [[True, 1], [1, 2]],
            [[float("nan"), 1], [1, 2]],
            [[1], [1, 2]],
        ):
            payload = collection_payload()
            payload["embeddings"] = value
            bad.append(payload)
        for field, value in (
            ("ids", ["a", "a"]),
            ("documents", ["", "policy"]),
            ("ids", [1, "a"]),
            ("documents", ["metric"]),
        ):
            payload = collection_payload()
            payload[field] = value
            bad.append(payload)
        for metadata in (
            {"content": "metric", "is_text_memory": False, "timestamp": "2026-10-02"},
            {"content": "changed", "is_text_memory": True, "timestamp": "2026-10-02"},
            {"content": "metric", "is_text_memory": 1, "timestamp": "2026-10-02"},
            {"content": "metric", "is_text_memory": True, "timestamp": "invalid"},
            {"content": "metric", "is_text_memory": True, "timestamp": "2026-10-02", "bad": {}},
        ):
            payload = collection_payload()
            payload["metadatas"][0] = metadata
            bad.append(payload)
        for number, payload in enumerate(bad):
            with self.subTest(number=number), self.assertRaises(ValueError):
                build_collection_evidence(payload)

    def test_empty_collection_is_valid_only_when_no_memory_text_is_registered(self):
        empty = {name: [] for name in ("ids", "documents", "metadatas", "embeddings")}
        evidence = build_collection_evidence(empty, index_records=[])
        self.assertEqual((evidence["count"], evidence["embedding_dimensions"]), (0, 0))
        with self.assertRaises(ValueError):
            build_collection_evidence(empty, index_records=index_records())

    def test_index_container_and_record_types_fail_closed_even_for_an_empty_collection(self):
        empty = {name: [] for name in ("ids", "documents", "metadatas", "embeddings")}
        for invalid in (
            {},
            (),
            "",
            1,
            [None],
            [{"content": "policy", "source_type": ""}],
            [{"content": "policy", "source_type": " metric_rule "}],
            [{"content": " ", "source_type": "metric_rule"}],
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                build_collection_evidence(empty, index_records=invalid)

    def test_timestamp_requires_time_but_accepts_vanna_naive_and_aware_datetimes(self):
        for timestamp in (
            "2026-10-03T10:00:00",
            "2026-10-03T10:00:00.123456",
            "2026-10-03T10:00:00+08:00",
        ):
            payload = collection_payload()
            payload["metadatas"][0]["timestamp"] = timestamp
            with self.subTest(timestamp=timestamp):
                self.assertEqual(build_collection_evidence(payload)["count"], 2)
        for timestamp in (
            "2026-10-03",
            " 2026-10-03T10:00:00",
            "2026-10-03T25:00:00",
            "2026-10-03T10:00:00 ",
        ):
            payload = collection_payload()
            payload["metadatas"][0]["timestamp"] = timestamp
            with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
                build_collection_evidence(payload)

    def test_same_text_multiple_ids_is_valid_but_changes_count_and_hash(self):
        payload = collection_payload()
        expected = build_collection_evidence(payload, index_records=index_records())
        for key, values in payload.items():
            payload[key] = list(values) + [deepcopy(values[0])]
        payload["ids"][-1] = "same-text-new-id"
        actual = build_collection_evidence(payload, index_records=index_records())
        self.assertEqual(actual["count"], expected["count"] + 1)
        with self.assertRaisesRegex(ValueError, "content changed"):
            assert_collection_evidence(actual, expected)

    def test_expected_evidence_requires_complete_strict_protocol(self):
        evidence = build_collection_evidence(collection_payload())
        assert_collection_evidence(evidence, deepcopy(evidence))
        for malformed in (
            {"sha256": evidence["sha256"]},
            evidence | {"count": True},
            evidence | {"schema_version": True},
            evidence | {"schema_version": 1},
            evidence | {"embedding_dimensions": 0},
            evidence | {"configuration_sha256": "not a digest"},
            evidence | {"sha256": "not a digest"},
            evidence | {"unexpected": True},
        ):
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                assert_collection_evidence(evidence, malformed)
        with self.assertRaisesRegex(ValueError, "content changed"):
            assert_collection_evidence(evidence, evidence | {"count": 3})

    def test_configuration_and_collection_metadata_are_content_addressed(self):
        expected = build_collection_evidence(collection_payload())
        cases = []
        for field, value in (("space", "l2"), ("ef_search", 200), ("future_option", True)):
            config = collection_configuration()
            config["hnsw"][field] = value
            cases.append((config, None))
        for field, value in (
            ("url", "http://other-host:11434/api/embeddings"),
            ("model_name", "other-model"),
            ("timeout", 240),
        ):
            config = collection_configuration()
            config["embedding_function"]["config"][field] = value
            cases.append((config, None))
        cases.extend(
            (
                (collection_configuration(), {}),
                (collection_configuration(), {"hnsw:search_ef": 200}),
            )
        )
        for config, metadata in cases:
            with self.subTest(config=config, metadata=metadata):
                actual = build_collection_evidence(
                    collection_payload(), configuration=config, collection_metadata=metadata
                )
                self.assertEqual(actual["count"], expected["count"])
                self.assertNotEqual(
                    actual["configuration_sha256"], expected["configuration_sha256"]
                )
                self.assertNotEqual(actual["sha256"], expected["sha256"])
                with self.assertRaises(ValueError):
                    assert_collection_evidence(actual, expected)
        self.assertNotIn("url", expected)
        self.assertNotIn("configuration", expected)

    def test_configuration_order_has_no_effect_but_values_are_never_coerced(self):
        configuration = collection_configuration()
        reordered = dict(reversed(list(configuration.items())))
        reordered["hnsw"] = dict(reversed(list(configuration["hnsw"].items())))
        expected = build_collection_evidence(collection_payload(), configuration=configuration)
        self.assertEqual(
            expected, build_collection_evidence(collection_payload(), configuration=reordered)
        )
        invalid = []
        for embedding in (
            None,
            {"type": "legacy"},
            {"type": "known", "name": "default", "config": {}},
        ):
            config = collection_configuration()
            config["embedding_function"] = embedding
            invalid.append(config)
        for field, value in (("ef_search", True), ("space", "unknown"), ("extra", float("nan"))):
            config = collection_configuration()
            config["hnsw"][field] = value
            invalid.append(config)
        for field, value in (
            ("url", "http://user:secret@localhost/api/embeddings"),
            ("url", "http://localhost/api/embeddings?token=secret"),
            ("model_name", " "),
            ("timeout", True),
        ):
            config = collection_configuration()
            config["embedding_function"]["config"][field] = value
            invalid.append(config)
        invalid.extend(({}, configuration | {"unknown_config": {}}))
        for config in invalid:
            with self.subTest(config=config), self.assertRaises(ValueError):
                build_collection_evidence(collection_payload(), configuration=config)
        with self.assertRaisesRegex(TypeError, "configuration"):
            _build_collection_evidence(collection_payload(), collection_metadata=None)
        with self.assertRaises(ValueError):
            build_collection_evidence(collection_payload(), collection_metadata={"unknown": {}})


class RuntimeCollectionEvidenceTests(unittest.TestCase):
    def test_existing_collection_is_read_without_creation_or_embedding(self):
        payload = collection_payload()
        runtime = RuntimeResources(load_settings())
        collection = stored_collection(runtime, payload=payload)
        client = Mock()
        client.get_collection.return_value = collection
        expected = build_collection_evidence(
            payload, configuration=collection.configuration_json, index_records=index_records()
        )
        with patch(
            "text2sql.infrastructure.runtime.chromadb.PersistentClient", return_value=client
        ):
            runtime.probe_knowledge_collection("versioned", expected, index_records=index_records())
        self.assertEqual(client.get_collection.call_count, 2)
        client.get_collection.assert_called_with("versioned", embedding_function=None)
        collection.get.assert_called_once_with(include=["documents", "metadatas", "embeddings"])
        client.create_collection.assert_not_called()
        collection.query.assert_not_called()
        client.close.assert_called_once()

    def test_missing_or_mutating_collection_fails_without_creating_it(self):
        client = Mock()
        runtime = RuntimeResources(load_settings())
        client.get_collection.side_effect = ValueError("missing collection")
        with (
            patch("text2sql.infrastructure.runtime.chromadb.PersistentClient", return_value=client),
            self.assertRaisesRegex(ValueError, "missing"),
        ):
            runtime.collection_evidence("missing")
        client.create_collection.assert_not_called()
        client.close.assert_called_once()
        collection = stored_collection(runtime, count=1)
        client.get_collection.side_effect = None
        client.get_collection.return_value = collection
        with (
            patch("text2sql.infrastructure.runtime.chromadb.PersistentClient", return_value=client),
            self.assertRaisesRegex(ValueError, "changed while reading"),
        ):
            runtime.collection_evidence("mutating")
        self.assertEqual(client.close.call_count, 2)

    def test_configuration_refetch_rejects_drift_not_visible_in_cached_collection(self):
        runtime = RuntimeResources(load_settings())
        before = stored_collection(runtime)
        after = stored_collection(runtime)
        after.configuration_json["hnsw"]["ef_search"] = 200
        for modified in (after, stored_collection(runtime)):
            if modified is not after:
                modified.metadata = {"hnsw:search_ef": 200}
            client = Mock()
            client.get_collection.side_effect = [before, modified]
            with (
                self.subTest(modified=modified),
                patch(
                    "text2sql.infrastructure.runtime.chromadb.PersistentClient", return_value=client
                ),
                self.assertRaisesRegex(ValueError, "changed while reading"),
            ):
                runtime.collection_evidence("versioned")
            self.assertEqual(client.get_collection.call_count, 2)
            client.close.assert_called_once()

    def test_stored_embedding_must_match_controlled_runtime_without_loading_it(self):
        runtime = RuntimeResources(load_settings())
        for key, value in (
            ("model_name", "wrong-model"),
            ("url", "http://other-host"),
            ("timeout", 999),
        ):
            collection = stored_collection(runtime)
            collection.configuration_json["embedding_function"]["config"][key] = value
            client = Mock()
            client.get_collection.return_value = collection
            with (
                self.subTest(key=key),
                patch(
                    "text2sql.infrastructure.runtime.chromadb.PersistentClient", return_value=client
                ),
                self.assertRaisesRegex(ValueError, "controlled runtime model"),
            ):
                runtime.collection_evidence("versioned")
            collection.get.assert_not_called()
            client.create_collection.assert_not_called()
            client.close.assert_called_once()


class ControlledMemoryTests(unittest.TestCase):
    def test_new_version_creation_uses_and_validates_the_owned_embedding(self):
        runtime = RuntimeResources(load_settings())
        collection = stored_collection(runtime)
        embedding = Mock()
        embedding.get_config.return_value = runtime._embedding_config()
        client = Mock()
        client.get_collection.side_effect = NotFoundError("missing")
        client.create_collection.return_value = collection
        memory = ChromaAgentMemory(collection_name="new-version", embedding_function=embedding)
        try:
            with patch.object(memory, "_get_client", return_value=client):
                self.assertIs(memory._get_collection(), collection)
            client.create_collection.assert_called_once_with(
                name="new-version",
                embedding_function=embedding,
                metadata={"description": "Tool usage memories for learning"},
            )
            embedding.assert_not_called()
        finally:
            memory._executor.shutdown(wait=False, cancel_futures=True)

    def test_memory_and_evidence_clients_share_the_same_nontelemetry_policy(self):
        runtime = RuntimeResources(load_settings())
        client = Mock()
        client.get_collection.return_value = stored_collection(runtime)
        embedding = Mock()
        embedding.get_config.return_value = runtime._embedding_config()
        memory = ChromaAgentMemory(
            persist_directory=runtime.config.knowledge_db_dir,
            collection_name="versioned",
            embedding_function=embedding,
        )
        try:
            with patch(
                "text2sql.infrastructure.runtime.chromadb.PersistentClient", return_value=client
            ) as factory:
                memory._get_collection()
                runtime.collection_evidence("versioned")
            self.assertEqual(factory.call_count, 2)
            first, second = factory.call_args_list
            self.assertEqual(first, second)
            self.assertIs(first.kwargs["settings"].anonymized_telemetry, False)
            self.assertIs(first.kwargs["settings"].allow_reset, False)
        finally:
            memory._executor.shutdown(wait=False, cancel_futures=True)

    def test_pinned_sdk_queries_use_the_owned_transport_and_expose_fresh_config(self):
        # An independent empty temporary database uses supplied vectors and a
        # mocked Ollama response: no project collection or model is contacted.
        embedding_config = collection_configuration()["embedding_function"]["config"]
        embedding = OllamaEmbeddingFunction(**embedding_config)
        embedding._client.embed = Mock(return_value={"embeddings": [[1.0, 2.0]]})
        memory = None
        try:
            with (
                TemporaryDirectory() as directory,
                chromadb.PersistentClient(
                    path=directory,
                    settings=ChromaSettings(anonymized_telemetry=False, allow_reset=False),
                ) as client,
            ):
                original = client.create_collection("owned-contract", embedding_function=embedding)
                original.upsert(ids=["one"], documents=["text"], embeddings=[[1.0, 2.0]])
                memory = ChromaAgentMemory(
                    persist_directory=directory,
                    collection_name="owned-contract",
                    embedding_function=embedding,
                )
                with patch.object(memory, "_get_client", return_value=client):
                    controlled = memory._get_collection()
                identity = normalize_collection_configuration(
                    controlled.configuration_json,
                    controlled.metadata,
                    expected_embedding=embedding_config,
                )
                self.assertEqual(identity["configuration"]["embedding_function"]["name"], "ollama")
                result = controlled.query(query_texts=["query"], n_results=1)
                self.assertEqual(result["ids"], [["one"]])
                embedding._client.embed.assert_called_once_with(
                    model=embedding_config["model_name"], input=["query"]
                )
                controlled.modify(configuration={"hnsw": {"ef_search": 200}})
                fresh = client.get_collection("owned-contract", embedding_function=None)
                self.assertEqual(fresh.configuration_json["hnsw"]["ef_search"], 200)
                self.assertNotEqual(identity["configuration"], fresh.configuration_json)
                client.delete_collection("owned-contract")
        finally:
            if memory is not None:
                memory._executor.shutdown(wait=False, cancel_futures=True)
            embedding._client._client.close()

    def test_existing_memory_explicitly_uses_owned_embedding_without_stored_instantiation(self):
        runtime = RuntimeResources(load_settings())
        collection = stored_collection(runtime)
        client = Mock()
        client.get_collection.return_value = collection
        embedding = Mock()
        embedding.get_config.return_value = runtime._embedding_config()
        memory = ChromaAgentMemory(collection_name="versioned", embedding_function=embedding)
        try:
            with patch.object(memory, "_get_client", return_value=client):
                self.assertIs(memory._get_collection(), collection)
                self.assertIs(memory._get_collection(), collection)
            self.assertEqual(client.get_collection.call_count, 2)
            client.get_collection.assert_any_call(name="versioned", embedding_function=None)
            client.get_collection.assert_any_call(name="versioned", embedding_function=embedding)
            client.create_collection.assert_not_called()
            embedding.assert_not_called()
        finally:
            memory._executor.shutdown(wait=False, cancel_futures=True)

    def test_unknown_stored_embedding_fails_before_explicit_open_or_create(self):
        runtime = RuntimeResources(load_settings())
        collection = stored_collection(runtime)
        collection.configuration_json["embedding_function"] = {"type": "legacy"}
        client = Mock()
        client.get_collection.return_value = collection
        embedding = Mock()
        embedding.get_config.return_value = runtime._embedding_config()
        memory = ChromaAgentMemory(collection_name="versioned", embedding_function=embedding)
        try:
            with (
                patch.object(memory, "_get_client", return_value=client),
                self.assertRaisesRegex(ValueError, "verifiable Ollama"),
            ):
                memory._get_collection()
            client.get_collection.assert_called_once_with(name="versioned", embedding_function=None)
            client.create_collection.assert_not_called()
            self.assertIsNone(memory._collection)
        finally:
            memory._executor.shutdown(wait=False, cancel_futures=True)

    def test_open_race_rejects_changed_storage_configuration(self):
        runtime = RuntimeResources(load_settings())
        before, after = stored_collection(runtime), stored_collection(runtime)
        after.configuration_json["hnsw"]["ef_search"] = 200
        embedding = Mock()
        embedding.get_config.return_value = runtime._embedding_config()
        memory = ChromaAgentMemory(collection_name="versioned", embedding_function=embedding)
        client = Mock()
        client.get_collection.side_effect = [before, after]
        try:
            with (
                patch.object(memory, "_get_client", return_value=client),
                self.assertRaisesRegex(ValueError, "changed while opening"),
            ):
                memory._get_collection()
            self.assertIsNone(memory._collection)
        finally:
            memory._executor.shutdown(wait=False, cancel_futures=True)
