"""Deterministic question semantics used to ground and validate Text2SQL."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
import re
from typing import Any


CITY_ALIASES = {
    "杭州": "杭州市",
    "杭州市": "杭州市",
    "宁波": "宁波市",
    "宁波市": "宁波市",
    "温州": "温州市",
    "温州市": "温州市",
}


@dataclass(frozen=True)
class MetricIntent:
    name: str
    aggregate: str
    column: str = "*"
    output_alias: str = "Value"


@dataclass(frozen=True)
class QuestionSemanticIR:
    original_question: str
    normalized_question: str
    metrics: tuple[MetricIntent, ...] = ()
    dimensions: tuple[str, ...] = ()
    entity_filters: dict[str, tuple[str, ...]] = field(default_factory=dict)
    date_start: str | None = None
    date_end: str | None = None
    expected_granularity: str = "aggregate"
    required_tables: tuple[str, ...] = ()
    ambiguities: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["metrics"] = [asdict(item) for item in self.metrics]
        payload["entity_filters"] = {
            key: list(values) for key, values in self.entity_filters.items()
        }
        return payload


def _dedupe(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for item in values if item))


def _extract_cities(question: str) -> tuple[str, ...]:
    cities: list[str] = []
    for alias, canonical in CITY_ALIASES.items():
        if alias in question:
            cities.append(canonical)
    for match in re.findall(r"([\u4e00-\u9fff]{2,8}市)", question):
        for prefix in ("请统计", "统计", "查询", "请问", "计算", "汇总"):
            if match.startswith(prefix):
                match = match[len(prefix) :]
                break
        if match not in {"城市", "各城市", "每个城市"}:
            cities.append(CITY_ALIASES.get(match, match))
    return _dedupe(cities)


def _extract_year_range(question: str) -> tuple[str | None, str | None]:
    years = [int(item) for item in re.findall(r"(?<!\d)(20\d{2})(?:年|度)?", question)]
    if years:
        year = years[0]
    elif "今年" in question or "本年" in question:
        year = date.today().year
    elif "去年" in question or "上年" in question:
        year = date.today().year - 1
    else:
        return None, None
    return f"{year:04d}-01-01", f"{year + 1:04d}-01-01"


def parse_question_semantics(question: str) -> QuestionSemanticIR:
    normalized = re.sub(r"\s+", " ", question).strip()
    metrics: list[MetricIntent] = []
    if any(token in normalized for token in ("人次", "次数", "数量")):
        metrics.append(MetricIntent("collection_count", "COUNT", "*", "Times"))
    if any(token in normalized for token in ("采集量", "采血量", "献血量", "血量")):
        metrics.append(MetricIntent("collection_volume", "SUM", "BCPVolume", "Volume"))

    dimensions: list[str] = []
    institution_dimension = bool(
        re.search(r"(?:每个|各|各个).*?(?:机构|单位|血站)", normalized)
        or any(token in normalized for token in ("机构维度", "按机构"))
    )
    city_dimension = bool(
        re.search(r"(?:每个|各|各个).*?(?:城市|地区|地市)", normalized)
        or any(token in normalized for token in ("城市维度", "按城市"))
    )
    if institution_dimension:
        dimensions.append("institution")
    if city_dimension:
        dimensions.append("city")

    entity_filters: dict[str, tuple[str, ...]] = {}
    cities = _extract_cities(normalized)
    if cities:
        entity_filters["city"] = cities

    blood_types: list[str] = []
    if "全血" in normalized:
        blood_types.append("0")
    if "成分血" in normalized or "机采" in normalized:
        blood_types.append("1")
    if blood_types:
        entity_filters["blood_type"] = _dedupe(blood_types)

    date_start, date_end = _extract_year_range(normalized)
    needs_org = bool(
        dimensions
        or cities
        or any(token in normalized for token in ("机构名称", "机构编号", "血站"))
    )
    required_tables = ["Stat_Collection"] if metrics or blood_types else []
    if needs_org:
        required_tables.append("Pub_OrgAddress")

    ambiguities: list[str] = []
    if not metrics and any(token in normalized for token in ("统计", "查询", "多少")):
        ambiguities.append("未明确统计指标（人次或采集量）")

    if institution_dimension:
        granularity = "one_row_per_institution"
    elif city_dimension:
        granularity = "one_row_per_city"
    elif len(blood_types) > 1:
        granularity = "one_row_per_blood_type"
    else:
        granularity = "aggregate"

    return QuestionSemanticIR(
        original_question=question,
        normalized_question=normalized,
        metrics=tuple(metrics),
        dimensions=_dedupe(dimensions),
        entity_filters=entity_filters,
        date_start=date_start,
        date_end=date_end,
        expected_granularity=granularity,
        required_tables=_dedupe(required_tables),
        ambiguities=tuple(ambiguities),
    )


def render_semantic_ir(ir: QuestionSemanticIR) -> str:
    import json

    return "【问题语义中间表示（必须逐项落实）】\n" + json.dumps(
        ir.to_dict(), ensure_ascii=False, indent=2
    )
