"""Fit and persist probability calibration for raw table-similarity scores."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path


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

    def predict(self, score: float) -> float:
        value = max(-40.0, min(40.0, self.slope * score + self.intercept))
        return 1.0 / (1.0 + math.exp(-value))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        expected_schema_fingerprint: str | None = None,
        expected_embedding_model: str | None = None,
        expected_dataset_fingerprint: str | None = None,
    ) -> PlattCalibrator | None:
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("status", "accepted") != "accepted":
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
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
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
    ) -> PlattCalibrator:
        if len(scores) != len(labels) or not scores or len(set(labels)) < 2:
            raise ValueError("校准训练需要同时包含正、负样本")
        slope = intercept = 0.0
        size = float(len(scores))
        for _ in range(iterations):
            gradient_slope = gradient_intercept = 0.0
            for score, label in zip(scores, labels, strict=True):
                value = max(-40.0, min(40.0, slope * score + intercept))
                probability = 1.0 / (1.0 + math.exp(-value))
                error = probability - label
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
    ) -> PlattCalibrator:
        return PlattCalibrator(
            slope=self.slope,
            intercept=self.intercept,
            threshold=self.threshold,
            target_recall=self.target_recall,
            artifact_version=1,
            status="accepted",
            schema_fingerprint=schema_fingerprint,
            embedding_model=embedding_model,
            dataset_fingerprint=dataset_fingerprint,
        )
