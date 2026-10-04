"""Fit and persist probability calibration for raw table-similarity scores."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

from ..knowledge.atomic import atomic_bytes


@dataclass(frozen=True)
class PlattCalibrator:
    slope: float
    intercept: float
    threshold: float
    target_recall: float
    artifact_version: int = 1
    status: str = "accepted"
    schema_fingerprint: str = ""
    embedding_model: str = ""
    dataset_fingerprint: str = ""
    business_card_fingerprint: str = ""
    embedding_model_digest: str = ""

    def predict(self, score: float) -> float:
        if not math.isfinite(score):
            raise ValueError("similarity score must be finite")
        value = max(-40.0, min(40.0, self.slope * score + self.intercept))
        return 1.0 / (1.0 + math.exp(-value))

    def save(self, path: Path) -> None:
        atomic_bytes(path, self.to_bytes())

    def to_bytes(self) -> bytes:
        return json.dumps(asdict(self), indent=2, allow_nan=False).encode("utf-8")

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        expected_schema_fingerprint: str | None = None,
        expected_embedding_model: str | None = None,
        expected_dataset_fingerprint: str | None = None,
        expected_business_card_fingerprint: str | None = None,
        expected_embedding_model_digest: str | None = None,
    ) -> PlattCalibrator | None:
        try:
            content = path.read_bytes()
        except OSError:
            return None
        return cls.from_bytes(
            content,
            expected_schema_fingerprint=expected_schema_fingerprint,
            expected_embedding_model=expected_embedding_model,
            expected_dataset_fingerprint=expected_dataset_fingerprint,
            expected_business_card_fingerprint=expected_business_card_fingerprint,
            expected_embedding_model_digest=expected_embedding_model_digest,
        )

    @classmethod
    def from_bytes(
        cls,
        content: bytes,
        *,
        expected_schema_fingerprint: str | None = None,
        expected_embedding_model: str | None = None,
        expected_dataset_fingerprint: str | None = None,
        expected_business_card_fingerprint: str | None = None,
        expected_embedding_model_digest: str | None = None,
    ) -> PlattCalibrator | None:
        """Validate exactly the bytes a caller will freeze into its artifact."""
        try:
            payload = json.loads(content)
            if not isinstance(payload, dict) or payload.get("status") != "accepted":
                return None
            if type(payload.get("artifact_version")) is not int or payload[
                "artifact_version"
            ] not in {1, 2}:
                return None
            values = [
                payload.get(name) for name in ("slope", "intercept", "threshold", "target_recall")
            ]
            if any(
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                for value in values
            ):
                return None
            if not 0 <= payload["threshold"] <= 1 or not 0 < payload["target_recall"] <= 1:
                return None
            for name in (
                "schema_fingerprint",
                "embedding_model",
                "dataset_fingerprint",
                "business_card_fingerprint",
                "embedding_model_digest",
            ):
                value = payload.get(name, "")
                if not isinstance(value, str) or value != value.strip():
                    return None
            if expected_schema_fingerprint and payload.get("schema_fingerprint") != (
                expected_schema_fingerprint
            ):
                return None
            if expected_embedding_model and payload.get("embedding_model") != (
                expected_embedding_model
            ):
                return None
            if expected_dataset_fingerprint and payload.get("dataset_fingerprint") != (
                expected_dataset_fingerprint
            ):
                return None
            if expected_business_card_fingerprint and payload.get("business_card_fingerprint") != (
                expected_business_card_fingerprint
            ):
                return None
            if (
                expected_embedding_model_digest
                and payload.get("embedding_model_digest") != expected_embedding_model_digest
            ):
                return None
            return cls(
                slope=float(payload["slope"]),
                intercept=float(payload["intercept"]),
                threshold=float(payload["threshold"]),
                target_recall=float(payload["target_recall"]),
                artifact_version=int(payload.get("artifact_version", 1)),
                status=str(payload.get("status", "accepted")),
                schema_fingerprint=str(payload.get("schema_fingerprint", "")),
                embedding_model=str(payload.get("embedding_model", "")),
                dataset_fingerprint=str(payload.get("dataset_fingerprint", "")),
                business_card_fingerprint=str(payload.get("business_card_fingerprint", "")),
                embedding_model_digest=str(payload.get("embedding_model_digest", "")),
            )
        except (TypeError, ValueError, OverflowError, UnicodeError):
            return None

    @classmethod
    def fit(
        cls,
        scores: list[float],
        labels: list[int],
        *,
        target_recall: float = 0.99,
        iterations: int = 2000,
        learning_rate: float = 0.05,
        sample_weights: list[float] | None = None,
    ) -> PlattCalibrator:
        if len(scores) != len(labels) or not scores or len(set(labels)) < 2:
            raise ValueError("校准训练需要同时包含正、负样本")
        if (
            any(type(label) is not int or label not in (0, 1) for label in labels)
            or any(not math.isfinite(score) for score in scores)
            or not math.isfinite(target_recall)
            or not 0 < target_recall <= 1
            or type(iterations) is not int
            or iterations < 1
            or not math.isfinite(learning_rate)
            or learning_rate <= 0
        ):
            raise ValueError(
                "calibration requires finite scores and valid binary labels/configuration"
            )
        weights = sample_weights if sample_weights is not None else [1.0] * len(scores)
        if len(weights) != len(scores) or any(
            not math.isfinite(weight) or weight <= 0 for weight in weights
        ):
            raise ValueError("样本权重必须与训练样本等长且为有限正数")
        slope = intercept = 0.0
        size = sum(weights)
        for _ in range(iterations):
            gradient_slope = gradient_intercept = 0.0
            for score, label, weight in zip(scores, labels, weights, strict=True):
                value = max(-40.0, min(40.0, slope * score + intercept))
                probability = 1.0 / (1.0 + math.exp(-value))
                error = (probability - label) * weight
                gradient_slope += error * score
                gradient_intercept += error
            slope -= learning_rate * gradient_slope / size
            intercept -= learning_rate * gradient_intercept / size

        probabilities = [
            1.0 / (1.0 + math.exp(-max(-40.0, min(40.0, slope * score + intercept))))
            for score in scores
        ]
        positive_probabilities = sorted(
            probability
            for probability, label in zip(probabilities, labels, strict=True)
            if label == 1
        )
        allowed_misses = int((1.0 - target_recall) * len(positive_probabilities))
        threshold = positive_probabilities[min(allowed_misses, len(positive_probabilities) - 1)]
        return cls(slope, intercept, threshold, target_recall)

    def calibrate_threshold(
        self,
        scores: list[float],
        labels: list[int],
        *,
        target_recall: float | None = None,
    ) -> PlattCalibrator:
        """Choose a threshold on a held-out calibration split."""
        if len(scores) != len(labels) or not scores or not any(labels):
            raise ValueError("阈值校准需要包含正样本的独立校准集")
        recall = self.target_recall if target_recall is None else target_recall
        if (
            any(type(label) is not int or label not in (0, 1) for label in labels)
            or any(not math.isfinite(score) for score in scores)
            or not math.isfinite(recall)
            or not 0 < recall <= 1
        ):
            raise ValueError("threshold calibration inputs are invalid")
        positives = sorted(
            self.predict(score) for score, label in zip(scores, labels, strict=True) if label
        )
        allowed_misses = int((1.0 - recall) * len(positives))
        threshold = positives[min(allowed_misses, len(positives) - 1)]
        return PlattCalibrator(self.slope, self.intercept, threshold, recall)

    def with_provenance(
        self,
        *,
        schema_fingerprint: str,
        embedding_model: str,
        dataset_fingerprint: str,
        business_card_fingerprint: str = "",
        embedding_model_digest: str = "",
    ) -> PlattCalibrator:
        return PlattCalibrator(
            slope=self.slope,
            intercept=self.intercept,
            threshold=self.threshold,
            target_recall=self.target_recall,
            artifact_version=2,
            status="accepted",
            schema_fingerprint=schema_fingerprint,
            embedding_model=embedding_model,
            dataset_fingerprint=dataset_fingerprint,
            business_card_fingerprint=business_card_fingerprint,
            embedding_model_digest=embedding_model_digest,
        )
