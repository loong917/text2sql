import json
from pathlib import Path

from src.domain.semantic_ir import SemanticCatalog

ROOT = Path(__file__).resolve().parents[1] / "knowledge" / "domain"


def _read(name: str):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


TEST_CATALOG = SemanticCatalog(
    metrics=tuple(_read("metrics.json")),
    dimensions=tuple(_read("dimensions.json")),
    entity_policies=tuple(
        item for item in _read("policies.json") if item.get("kind") == "entity_filter"
    ),
    joins=tuple(_read("joins.json")),
    entities=tuple(_read("entities.json")),
)
