"""Structured, versioned domain knowledge for Text2SQL."""

from .structured import (
    KnowledgeBundle,
    KnowledgeValidationError,
    load_knowledge_bundle,
    load_validated_knowledge_bundle,
)

__all__ = [
    "KnowledgeBundle",
    "KnowledgeValidationError",
    "load_knowledge_bundle",
    "load_validated_knowledge_bundle",
]
