"""Fit and persist probability calibration for raw table-similarity scores."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path


@dataclass(frozen=True)
class PlattCalibrator:
    slope: float
    intercept: float
    threshold: float
    target_recall: float

    def predict(self, score: float) -> float:
        value = max(-40.0, min(40.0, self.slope * score + self.intercept))
        return 1.0 / (1.0 + math.exp(-value))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "PlattCalibrator | None":
        if not path.exists():
            return None
        try:
            return cls(**json.loads(path.read_text(encoding="utf-8")))
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
    ) -> "PlattCalibrator":
        if len(scores) != len(labels) or not scores or len(set(labels)) < 2:
            raise ValueError("校准训练需要同时包含正、负样本")
        slope = intercept = 0.0
        size = float(len(scores))
        for _ in range(iterations):
            gradient_slope = gradient_intercept = 0.0
            for score, label in zip(scores, labels):
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
            for probability, label in zip(probabilities, labels)
            if label == 1
        )
        allowed_misses = int((1.0 - target_recall) * len(positive_probabilities))
        threshold = positive_probabilities[
            min(allowed_misses, len(positive_probabilities) - 1)
        ]
        return cls(slope, intercept, threshold, target_recall)
