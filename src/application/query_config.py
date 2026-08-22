"""Explicit configuration consumed by the online Text2SQL use case."""

from dataclasses import dataclass


@dataclass(frozen=True)
class QueryServiceConfig:
    max_result_rows: int
    query_timeout_seconds: float
    feedback_min_result_rows: int
