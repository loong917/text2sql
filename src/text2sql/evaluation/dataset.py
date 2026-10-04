"""Load approved evaluation cases and enforce split integrity."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from sqlglot import exp, parse
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.scope import Scope, traverse_scope

from ..domain.semantic_contract import validate_semantic_snapshot
from ..domain.sql_validation import FORBIDDEN_NODES
from ..knowledge.governance import ExecutionEvidence, ReviewEvidence, content_digest, sql_digest
from .comparison import ResultComparison
from .sql_assertions import SqlAssertions

VALID_SPLITS = {
    "retrieval_train",
    "retrieval_calibration",
    "retrieval_test",
    "dev",
    "test",
}


@dataclass(frozen=True)
class EvaluationCase:
    id: str
    split: str
    template_id: str
    template_fingerprint: str | None
    question: str
    payload: dict[str, Any]


def _normalize_template_bindings(tree: exp.Expression) -> None:
    """Rename relational bindings by scope and source position, not authored spelling.

    Physical table/schema identities stay intact. A CTE or derived relation's
    output columns are identified by projection position, preserving lineage
    even when its presentation labels or local aliases change.
    """
    scopes = traverse_scope(tree)
    indices = {id(scope): index for index, scope in enumerate(scopes)}
    expressions = {id(scope.expression): scope for scope in scopes}
    sources = {id(scope): list(scope.selected_sources.items()) for scope in scopes}
    aliases = {
        id(scope): {
            alias.casefold(): (f"_s{indices[id(scope)]}_{index}", source)
            for index, (alias, (_, source)) in enumerate(sources[id(scope)])
        }
        for scope in scopes
    }
    outputs: dict[int, dict[str, str]] = {}
    projection_labels: dict[int, dict[str, str]] = {}
    for scope in scopes:
        labels = list(scope.outer_columns or scope.expression.named_selects)
        outputs[id(scope)] = {
            label.casefold(): f"_o{index}" for index, label in enumerate(labels) if label != "*"
        }
        projection_labels[id(scope)] = {
            label.casefold(): f"_o{index}"
            for index, label in enumerate(scope.expression.named_selects)
            if label != "*"
        }

    # Resolve every column against the original bindings before changing names.
    for column in tree.find_all(exp.Column):
        owner = column.parent
        while owner is not None and id(owner) not in expressions:
            owner = owner.parent
        if owner is None:
            continue
        scope = expressions[id(owner)]
        local = aliases[id(scope)]
        if not column.table:
            order = column.find_ancestor(exp.Order)
            if (
                order is not None
                and order.parent is scope.expression
                and column.find_ancestor(exp.Window) is None
                and column.name.casefold() in projection_labels[id(scope)]
            ):
                column.set(
                    "this", exp.to_identifier(projection_labels[id(scope)][column.name.casefold()])
                )
                continue
            binding = next(iter(local.values())) if len(local) == 1 else None
        else:
            binding = local.get(column.table.casefold())
            parent: Scope | None = scope.parent
            while binding is None and parent is not None:
                binding = aliases[id(parent)].get(column.table.casefold())
                parent = parent.parent
        if binding is not None:
            alias, source = binding
            if isinstance(source, Scope):
                label = outputs[id(source)].get(column.name.casefold())
                if label is not None:
                    column.set("this", exp.to_identifier(label))
            column.set("table", exp.to_identifier(alias))

    for scope in scopes:
        for index, (_, (node, source)) in enumerate(sources[id(scope)]):
            canonical = f"_s{indices[id(scope)]}_{index}"
            if isinstance(node, exp.Table):
                if isinstance(source, Scope):
                    node.set("this", exp.to_identifier(f"_cte{indices[id(source)]}"))
                elif not node.db:
                    node.set("db", exp.to_identifier("dbo"))
                node.set("alias", exp.TableAlias(this=exp.to_identifier(canonical)))
            elif isinstance(source, Scope):
                wrapper = source.expression.parent
                if isinstance(wrapper, (exp.Subquery, exp.Lateral)):
                    wrapper.set("alias", exp.TableAlias(this=exp.to_identifier(canonical)))
        if isinstance(scope.expression.parent, exp.CTE):
            scope.expression.parent.set(
                "alias",
                exp.TableAlias(
                    this=exp.to_identifier(f"_cte{indices[id(scope)]}"),
                    columns=None,
                ),
            )
        if isinstance(scope.expression, exp.Select):
            scope.expression.set(
                "expressions",
                [
                    projection
                    if isinstance(projection, exp.Star)
                    or isinstance(projection, exp.Column)
                    and projection.is_star
                    else exp.alias_(
                        projection.this if isinstance(projection, exp.Alias) else projection,
                        f"_o{index}",
                    )
                    for index, projection in enumerate(scope.expression.expressions)
                ],
            )


def sql_template_fingerprint(sql: str) -> str:
    """Return a value-independent fingerprint for one T-SQL query shape.

    Literal values and presentation-only output aliases are deliberately
    removed.  Consequently, changing a year, city, blood type, or result label
    cannot hide that two evaluation records share the same SQL template.
    """
    try:
        statements = parse(sql, read="tsql")
    except ParseError as exc:
        raise ValueError(f"baseline_sql is not valid T-SQL: {exc}") from exc
    if len(statements) != 1 or not isinstance(
        statements[0], (exp.Select, exp.Union, exp.Intersect, exp.Except)
    ):
        raise ValueError("baseline_sql must be a single read-only T-SQL query")
    tree = statements[0]
    if any(tree.find(node) is not None for node in FORBIDDEN_NODES):
        raise ValueError("baseline_sql contains a write operation or dangerous SQL")
    try:
        _normalize_template_bindings(tree)
    except OptimizeError as exc:
        raise ValueError(f"baseline_sql has invalid scope bindings: {exc}") from exc

    def normalize(node: exp.Expression) -> exp.Expression:
        if isinstance(node, (exp.Literal, exp.National)):
            return exp.Literal.string("__value__")
        return node

    canonical = tree.transform(normalize).sql(
        dialect="tsql",
        normalize=True,
        pretty=False,
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _read_records(content: bytes, path: Path) -> list[dict[str, Any]]:
    text = content.decode("utf-8")
    if path.suffix.lower() == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    payload = json.loads(text)
    if not isinstance(payload, list):
        raise ValueError(f"{path}: JSON root must be an array")
    return payload


def load_evaluation_cases(
    path: str | Path,
    *,
    expected_split: str | None = None,
) -> list[EvaluationCase]:
    source = Path(path)
    if not source.exists():
        return []
    return load_evaluation_cases_bytes(
        source.read_bytes(), source=source, expected_split=expected_split
    )


def load_evaluation_cases_bytes(
    content: bytes,
    *,
    source: str | Path,
    expected_split: str | None = None,
) -> list[EvaluationCase]:
    """Validate precisely the dataset bytes bound into release evidence."""
    source = Path(source)
    cases: list[EvaluationCase] = []
    seen_ids: set[str] = set()
    seen_questions: set[str] = set()
    for line_number, payload in enumerate(_read_records(content, source), 1):
        if not isinstance(payload, dict):
            raise ValueError(f"{source}:{line_number}: record must be an object")
        for key in ("id", "split", "template_id", "question", "status"):
            if not isinstance(payload.get(key), str):
                raise ValueError(f"{source}:{line_number}: {key} must be a string")
        case_id = payload["id"].strip()
        split = payload["split"].strip()
        template_id = payload["template_id"].strip()
        question = payload["question"].strip()
        status = payload["status"].strip()
        if not case_id or not template_id or not question:
            raise ValueError(f"{source}:{line_number}: id, template_id and question are required")
        if split not in VALID_SPLITS:
            raise ValueError(f"{source}:{line_number}: invalid split '{split}'")
        if expected_split and split != expected_split:
            raise ValueError(
                f"{source}:{line_number}: expected split '{expected_split}', got '{split}'"
            )
        if status != "approved":
            raise ValueError(f"{source}:{line_number}: only approved cases are allowed")
        if not isinstance(payload.get("should_refuse"), bool):
            raise ValueError(f"{source}:{line_number}: should_refuse must be a boolean")
        should_refuse = payload["should_refuse"]
        if "must_execute" in payload and not isinstance(payload["must_execute"], bool):
            raise ValueError(f"{source}:{line_number}: must_execute must be a boolean")
        if split == "test" and not should_refuse and payload.get("must_execute") is not True:
            raise ValueError(
                f"{source}:{line_number}: positive Test cases require explicit must_execute=true"
            )
        outcome = payload.get("expected_outcome", "refused" if should_refuse else "success")
        allowed_outcomes = {"refused", "clarification_required"} if should_refuse else {"success"}
        if not isinstance(outcome, str) or outcome not in allowed_outcomes:
            raise ValueError(
                f"{source}:{line_number}: expected_outcome conflicts with should_refuse"
            )
        for key in (
            "must_include_tables",
            "must_include_columns",
            "must_include_filters",
            "must_include_joins",
            "must_not_contain",
            "must_not_include_columns",
            "expected_result_columns",
        ):
            if key in payload and (
                not isinstance(payload[key], list)
                or not all(isinstance(value, str) and value for value in payload[key])
                or len(payload[key]) != len(set(payload[key]))
            ):
                raise ValueError(
                    f"{source}:{line_number}: {key} must contain unique non-empty strings"
                )
        if "must_have_group_by" in payload and not isinstance(payload["must_have_group_by"], bool):
            raise ValueError(f"{source}:{line_number}: must_have_group_by must be a boolean")
        for key in ("min_result_rows", "max_result_rows"):
            if key in payload and (type(payload[key]) is not int or payload[key] < 0):
                raise ValueError(f"{source}:{line_number}: {key} must be a non-negative integer")
        try:
            ResultComparison.from_case(payload)
        except ValueError as exc:
            raise ValueError(f"{source}:{line_number}: {exc}") from exc
        if "baseline_sql" in payload and not isinstance(payload["baseline_sql"], str):
            raise ValueError(f"{source}:{line_number}: baseline_sql must be a string")
        baseline_sql = str(payload.get("baseline_sql") or "").strip()
        if not should_refuse and not baseline_sql:
            raise ValueError(f"{source}:{line_number}: baseline_sql is required for positive cases")
        if split in {"dev", "test"}:
            semantic_errors = validate_semantic_snapshot(payload.get("expected_semantic_ir"))
            if semantic_errors:
                raise ValueError(
                    f"{source}:{line_number}: expected_semantic_ir invalid: "
                    + "; ".join(semantic_errors)
                )
        normalized_question = " ".join(question.lower().split())
        if case_id in seen_ids:
            raise ValueError(f"{source}:{line_number}: duplicate id '{case_id}'")
        if normalized_question in seen_questions:
            raise ValueError(f"{source}:{line_number}: duplicate question")
        seen_ids.add(case_id)
        seen_questions.add(normalized_question)
        try:
            template_fingerprint = sql_template_fingerprint(baseline_sql) if baseline_sql else None
            if baseline_sql and not should_refuse:
                failed = [
                    item["name"]
                    for item in SqlAssertions(baseline_sql).checks(payload)
                    if not item["passed"]
                ]
                if failed:
                    raise ValueError(
                        "baseline_sql conflicts with declared case constraints: "
                        + ", ".join(failed)
                    )
        except ValueError as exc:
            raise ValueError(f"{source}:{line_number}: {exc}") from exc
        try:
            review = ReviewEvidence.model_validate(payload.get("review"))
            if review.content_sha256 != content_digest(payload):
                raise ValueError("review evidence does not match current authored content")
        except ValueError as exc:
            raise ValueError(f"{source}:{line_number}: review evidence invalid: {exc}") from exc
        if not should_refuse:
            try:
                execution = ExecutionEvidence.model_validate(payload.get("execution"))
            except ValueError as exc:
                raise ValueError(
                    f"{source}:{line_number}: execution evidence invalid: {exc}"
                ) from exc
            if execution.baseline_sha256 != sql_digest(baseline_sql):
                raise ValueError(
                    f"{source}:{line_number}: execution evidence does not match current baseline_sql"
                )
            if execution.schema_fingerprint != review.schema_fingerprint:
                raise ValueError(
                    f"{source}:{line_number}: review and execution Schema fingerprints disagree"
                )
        cases.append(
            EvaluationCase(
                case_id,
                split,
                template_id,
                template_fingerprint,
                question,
                payload,
            )
        )
    return cases


def assert_disjoint_splits(*groups: list[EvaluationCase]) -> None:
    seen_ids: set[str] = set()
    seen_questions: set[str] = set()
    seen_templates: set[str] = set()
    fingerprint_owners: dict[str, str] = {}
    for cases in groups:
        group_ids: set[str] = set()
        group_questions: set[str] = set()
        group_templates: set[str] = set()
        group_fingerprints: dict[str, str] = {}
        for case in cases:
            normalized = " ".join(case.question.lower().split())
            fingerprint_owner = (
                fingerprint_owners.get(case.template_fingerprint)
                if case.template_fingerprint
                else None
            )
            if (
                case.id in seen_ids
                or normalized in seen_questions
                or case.template_id in seen_templates
            ):
                raise ValueError(f"evaluation split leakage detected: {case.id}")
            if fingerprint_owner:
                raise ValueError(
                    f"evaluation SQL template leakage detected: {fingerprint_owner} <-> {case.id}"
                )
            group_ids.add(case.id)
            group_questions.add(normalized)
            group_templates.add(case.template_id)
            if case.template_fingerprint:
                group_fingerprints.setdefault(case.template_fingerprint, case.id)
        seen_ids.update(group_ids)
        seen_questions.update(group_questions)
        seen_templates.update(group_templates)
        fingerprint_owners.update(group_fingerprints)
