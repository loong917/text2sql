"""Canonical Gold lifecycle, independent evidence, isolation and immutable export."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlglot import exp, parse
from sqlglot.errors import ErrorLevel, ParseError, SqlglotError, TokenError

from ..domain.sql_validation import FORBIDDEN_NODES, SqlSafetyPolicy, validate_tsql_ast
from ..knowledge.atomic import atomic_bytes, atomic_json
from ..knowledge.governance import ExecutionEvidence, ReviewEvidence, content_digest, sql_digest
from ..knowledge.models import (
    RECORD_MODELS,
    GoldSqlRecord,
    KnowledgeManifest,
    NegativeSqlRecord,
    RefusalRecord,
)
from ..knowledge.provenance import schema_fingerprint
from ..knowledge.schema_contract import require_schema_contract, validate_schema_contract
from ..knowledge.structured import load_validated_knowledge_bundle
from .dataset import (
    VALID_SPLITS,
    EvaluationCase,
    assert_disjoint_splits,
    load_evaluation_cases,
    load_evaluation_cases_bytes,
    sql_template_fingerprint,
)

Purpose = Literal[
    "prompt_gold",
    "negative",
    "refusal",
    "migration_review",
    "retrieval_train",
    "retrieval_calibration",
    "retrieval_test",
    "dev",
    "test",
]
EXPORT_PATHS = {
    "prompt_gold": "knowledge/examples/gold_sql.jsonl",
    "negative": "knowledge/examples/negative_sql.jsonl",
    "refusal": "knowledge/examples/refusal.jsonl",
    **{split: f"evaluation/{split}.jsonl" for split in sorted(VALID_SPLITS)},
}
HELD_OUT = {"dev", "test", "retrieval_calibration", "retrieval_test"}
KNOWLEDGE_FILES = (
    "knowledge/manifest.json",
    "knowledge/schema/table_cards.jsonl",
    *(
        f"knowledge/domain/{name}.json"
        for name in ("metrics", "dimensions", "joins", "policies", "entities")
    ),
)
KNOWLEDGE_ATTRIBUTES = {
    "knowledge/schema/table_cards.jsonl": "table_cards",
    **{
        f"knowledge/domain/{name}.json": name
        for name in ("metrics", "dimensions", "joins", "policies", "entities")
    },
}


class GoldCase(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", str_min_length=1)
    id: str
    question: str
    purpose: Purpose
    query_family_id: str
    category: str
    difficulty: Literal["easy", "medium", "hard", "unknown"]
    source: str
    source_line: int = Field(ge=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["candidate", "approved", "rejected"]
    payload: dict[str, Any]
    review: ReviewEvidence | None = None
    execution: ExecutionEvidence | None = None

    @model_validator(mode="after")
    def reviewed_approval(self) -> GoldCase:
        if self.status == "approved":
            if self.purpose == "migration_review" or self.review is None:
                raise ValueError("approval requires a final purpose and independent reviewers")
            if self.payload.get("question") != self.question:
                raise ValueError("canonical question and payload disagree")
            if self.review.content_sha256 != content_digest(self.export_payload()):
                raise ValueError("review evidence does not match current exported content")
            positive = self.purpose == "prompt_gold" or (
                self.purpose in VALID_SPLITS and self.payload.get("should_refuse") is False
            )
            if positive:
                sql = self.payload.get("sql", self.payload.get("baseline_sql"))
                if not isinstance(sql, str) or self.execution is None:
                    raise ValueError("positive approval requires executed baseline evidence")
                if self.execution.baseline_sha256 != sql_digest(sql):
                    raise ValueError("execution evidence does not match current SQL")
                if self.execution.schema_fingerprint != self.review.schema_fingerprint:
                    raise ValueError("review and execution Schema fingerprints disagree")
        return self

    def export_payload(self) -> dict[str, Any]:
        payload = dict(self.payload)
        payload.update(status="approved", question=self.question)
        payload["review"] = self.review.model_dump() if self.review else None
        if self.execution:
            payload["execution"] = self.execution.model_dump()
        if self.purpose in VALID_SPLITS:
            payload.update(split=self.purpose, template_id=self.query_family_id)
        return payload


def load_gold_cases(root: str | Path) -> list[GoldCase]:
    path = Path(root) / "cases.jsonl"
    return load_gold_cases_bytes(path.read_bytes(), source=path)


def load_gold_cases_bytes(content: bytes, *, source: str | Path) -> list[GoldCase]:
    path = Path(source)
    cases: list[GoldCase] = []
    ids: set[str] = set()
    for line, record in enumerate(content.decode("utf-8").splitlines(), 1):
        if not record.strip():
            continue
        try:
            case = GoldCase.model_validate_json(record, strict=True)
        except ValidationError as exc:
            raise ValueError(f"{path}:{line}: {exc}") from exc
        if case.id in ids:
            raise ValueError(f"{path}:{line}: duplicate canonical id {case.id}")
        ids.add(case.id)
        cases.append(case)
    return cases


def _negative_template_fingerprint(sql: str, error_types: Any) -> str | None:
    """Exempt only parser-confirmed syntax negatives, never an authored label alone."""
    try:
        fingerprint = sql_template_fingerprint(sql)
        tree = parse(sql, read="tsql")[0]
        if tree is None:
            raise ValueError("cannot safely isolate an empty negative SQL statement")
        # The pinned parser accepts a bare GROUP BY as an empty Group node.
        # Successful parsing alone is not proof of a complete query shape.
        if any(node.error_messages() for node in tree.walk()) or any(
            not node.expressions
            and (
                isinstance(node, exp.Order)
                or not any(node.args.get(key) for key in ("grouping_sets", "cube", "rollup"))
            )
            for node in tree.find_all(exp.Group, exp.Order)
        ):
            raise ValueError("cannot safely isolate negative SQL with incomplete query clauses")
        return fingerprint
    except TokenError as exc:
        raise ValueError(f"negative SQL has invalid tokenization: {exc}") from exc
    except ValueError:
        if isinstance(error_types, list) and "syntax_error" in error_types:
            try:
                parse(sql, read="tsql")
            except ParseError:
                # Do not repair malformed clauses into a different template.
                # A recognizable query body could expose a held-out query even
                # when a damaged suffix makes strict parsing fail.
                try:
                    recovered = parse(sql, read="tsql", error_level=ErrorLevel.IGNORE)
                except SqlglotError as exc:
                    raise ValueError("negative SQL cannot be safely tokenized") from exc
                if (
                    len(recovered) == 1
                    and isinstance(recovered[0], (exp.Select, exp.Union, exp.Intersect, exp.Except))
                    and not any(recovered[0].find(node) for node in FORBIDDEN_NODES)
                ):
                    tree = recovered[0]
                    complete_projection = any(
                        not any(part.error_messages() for part in projection.walk())
                        for select in tree.find_all(exp.Select)
                        for projection in select.expressions
                    )
                    if tree.find(exp.Table) is not None or complete_projection:
                        raise ValueError(
                            "cannot safely isolate syntax-error negative with a recognizable "
                            "query body; retain it for review or independent safety tests"
                        ) from None
                    return None
        raise


def isolation_errors(cases: list[GoldCase]) -> list[str]:
    """Hold out SQL shapes and declared families, not merely question spelling."""
    owners: dict[tuple[str, str], tuple[str, str]] = {}
    errors: list[str] = []
    for case in cases:
        if case.purpose == "migration_review":
            continue
        group = case.purpose if case.purpose in HELD_OUT else "training"
        keys = [
            ("question", " ".join(case.question.casefold().split())),
            ("family", case.query_family_id),
        ]
        sql = case.payload.get("sql", case.payload.get("baseline_sql"))
        if isinstance(sql, str) and (sql.strip() or case.purpose == "negative"):
            try:
                fingerprint = (
                    _negative_template_fingerprint(sql, case.payload.get("error_types"))
                    if case.purpose == "negative"
                    else sql_template_fingerprint(sql)
                )
                if fingerprint is not None:
                    keys.append(("sql_template", fingerprint))
            except ValueError as exc:
                errors.append(f"{case.id}: {exc}")
        for key in keys:
            previous = owners.get(key)
            if previous and previous[0] != group:
                errors.append(f"{key[0]} leakage: {previous[1]} <-> {case.id}")
            owners.setdefault(key, (group, case.id))
    return sorted(set(errors))


def _export_views(cases: list[GoldCase]) -> dict[str, bytes]:
    """The single canonical-to-view transformation for export and verification."""
    approved = sorted(
        (case for case in cases if case.status == "approved"), key=lambda case: case.id
    )
    return {
        relative: "".join(
            json.dumps(case.export_payload(), ensure_ascii=False, sort_keys=True) + "\n"
            for case in approved
            if case.purpose == purpose
        ).encode("utf-8")
        for purpose, relative in EXPORT_PATHS.items()
    }


def _view_contract_errors(views: dict[str, bytes]) -> list[str]:
    """Validate complete split groups, including duplicates, not singleton cases."""
    errors: list[str] = []
    models: dict[str, type[GoldSqlRecord] | type[NegativeSqlRecord] | type[RefusalRecord]] = {
        "prompt_gold": GoldSqlRecord,
        "negative": NegativeSqlRecord,
        "refusal": RefusalRecord,
    }
    for purpose, relative in EXPORT_PATHS.items():
        try:
            data = views[relative]
            if purpose in VALID_SPLITS:
                load_evaluation_cases_bytes(data, source=relative, expected_split=purpose)
            else:
                ids: set[str] = set()
                for line in data.decode("utf-8").splitlines():
                    if not line.strip():
                        continue
                    record = models[purpose].model_validate_json(line, strict=True)
                    if record.status != "approved":
                        raise ValueError("runtime views may contain only approved records")
                    if record.id in ids:
                        raise ValueError(f"duplicate exported id {record.id}")
                    ids.add(record.id)
        except (ValueError, KeyError) as exc:
            errors.append(f"{relative}: {exc}")
    return errors


def _knowledge_contract_errors(contents: dict[str, bytes], digest: str) -> list[str]:
    errors: list[str] = []
    try:
        manifest = json.loads(contents["knowledge/manifest.json"])
        if not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int:
            raise ValueError("knowledge schema_version must be an explicit integer")
        KnowledgeManifest.model_validate(manifest)
    except (ValueError, KeyError) as exc:
        errors.append(f"knowledge/manifest.json: {exc}")
    for relative, attribute in KNOWLEDGE_ATTRIBUTES.items():
        try:
            data = contents[relative]
            if relative.endswith(".jsonl"):
                values = [
                    json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()
                ]
            else:
                values = json.loads(data)
            if not isinstance(values, list):
                raise ValueError("knowledge root must be an array")
            if attribute in {"metrics", "table_cards"} and not values:
                raise ValueError("reviewed metric definitions and table cards are required")
            identities: set[str] = set()
            for value in values:
                record = RECORD_MODELS[attribute].model_validate(value).model_dump()
                review = record.get("review")
                if not review or review["schema_fingerprint"] != digest:
                    raise ValueError("knowledge review must match the generation Schema")
                identity = record.get("id", record.get("table"))
                if not isinstance(identity, str):
                    raise ValueError("knowledge records require an identity")
                if identity in identities:
                    raise ValueError(f"duplicate knowledge id {identity}")
                identities.add(identity)
                if attribute == "table_cards" and not str(record.get("grain") or "").strip():
                    raise ValueError("reviewed table cards require a fact grain")
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(f"{relative}: {exc}")
    return errors


def validate_approved_cases(
    cases: list[GoldCase], schema: dict[str, dict[str, Any]], policy: SqlSafetyPolicy
) -> list[str]:
    require_schema_contract(schema)
    digest = schema_fingerprint(schema)
    errors = isolation_errors(cases) + _view_contract_errors(_export_views(cases))
    for case in cases:
        payload = case.export_payload()
        if case.review is None or case.review.schema_fingerprint != digest:
            errors.append(f"{case.id}: review belongs to another Schema")
            continue
        try:
            if case.purpose in VALID_SPLITS:
                load_evaluation_cases_bytes(
                    (json.dumps(payload, ensure_ascii=False) + "\n").encode(),
                    source=f"{case.purpose}.jsonl",
                    expected_split=case.purpose,
                )
            elif case.purpose == "prompt_gold":
                GoldSqlRecord.model_validate(payload)
            elif case.purpose == "negative":
                NegativeSqlRecord.model_validate(payload)
            elif case.purpose == "refusal":
                RefusalRecord.model_validate(payload)
            else:
                raise ValueError("nonexportable purpose")
            sql = payload.get("sql", payload.get("baseline_sql"))
            if isinstance(sql, str) and case.purpose != "negative":
                sql_template_fingerprint(sql)
                error = validate_tsql_ast(sql, schema, safety_policy=policy)
                if error:
                    raise ValueError(error)
            if case.execution and case.execution.schema_fingerprint != digest:
                raise ValueError("execution belongs to another Schema")
        except (ValueError, ValidationError) as exc:
            errors.append(f"{case.id}: {exc}")
    return errors


def audit_gold_set(cases: list[GoldCase]) -> dict[str, Any]:
    approved = [case for case in cases if case.status == "approved"]
    contract_errors = _view_contract_errors(_export_views(cases))
    counts = Counter(case.status for case in cases)
    purposes = Counter(case.purpose for case in approved)
    test = [case for case in approved if case.purpose == "test"]
    positives = sum(case.payload.get("should_refuse") is False for case in test)
    negatives = sum(case.payload.get("should_refuse") is True for case in test)
    deficits = {
        "test_total": max(0, 100 - len(test)),
        "test_positive": max(0, 80 - positives),
        "test_negative": max(0, 20 - negatives),
        "retrieval_calibration": max(0, 30 - purposes["retrieval_calibration"]),
        "retrieval_test": max(0, 30 - purposes["retrieval_test"]),
    }
    return {
        "total": len(cases),
        "states": dict(counts),
        "approved_purposes": dict(purposes),
        "production_minimum_deficits": deficits,
        "approved_isolation_errors": isolation_errors(approved),
        "approved_contract_errors": contract_errors,
        "draft_isolation_findings": isolation_errors(cases),
        "production_data_ready": bool(approved)
        and not any(deficits.values())
        and not isolation_errors(approved)
        and not contract_errors,
        "categories": dict(Counter(case.category for case in cases)),
    }


def export_gold_set(
    root: str | Path,
    destination: str | Path,
    schema: dict[str, dict[str, Any]],
    policy: SqlSafetyPolicy,
    *,
    knowledge_root: str | Path | None = None,
) -> Path:
    """Write a new immutable generation; never modify runtime views in place.

    The destination must not exist. Consumers select the whole generation;
    manifest digests bind all eight files and the canonical input bytes.
    """
    root, destination = Path(root), Path(destination)
    content = (root / "cases.jsonl").read_bytes()
    cases = load_gold_cases_bytes(content, source=root / "cases.jsonl")
    approved = [case for case in cases if case.status == "approved"]
    if not approved:
        raise ValueError("no reviewed approved cases; candidates cannot be exported")
    errors = validate_approved_cases(approved, schema, policy)
    if errors:
        raise ValueError("; ".join(errors))
    knowledge_bytes: dict[str, bytes] = {}
    if knowledge_root is not None:
        load_validated_knowledge_bundle(knowledge_root, schema, require_reviewed=True)
        for relative in KNOWLEDGE_FILES:
            knowledge_bytes[relative] = (
                Path(knowledge_root) / Path(relative).relative_to("knowledge")
            ).read_bytes()
    destination.mkdir(parents=True, exist_ok=False)
    hashes: dict[str, str] = {}
    for relative, data in knowledge_bytes.items():
        assert knowledge_root is not None
        atomic_bytes(destination / relative, data)
        hashes[relative] = sha256(data).hexdigest()
    for relative, data in _export_views(cases).items():
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        hashes[relative] = sha256(data).hexdigest()
    if knowledge_root is not None:
        # Revalidate the exact frozen bytes including the newly selected Gold,
        # not merely the mutable source directory inspected earlier.
        load_validated_knowledge_bundle(destination / "knowledge", schema, require_reviewed=True)
    if content != (root / "cases.jsonl").read_bytes():
        raise ValueError("Gold Set changed during export; generation is incomplete and unusable")
    for relative, data in knowledge_bytes.items():
        assert knowledge_root is not None
        if (Path(knowledge_root) / Path(relative).relative_to("knowledge")).read_bytes() != data:
            raise ValueError("reviewed knowledge changed during export; generation is incomplete")
    atomic_bytes(destination / "gold-cases.jsonl", content)
    atomic_json(
        destination / "gold-manifest.json",
        {
            "schema_version": 1,
            "canonical_sha256": sha256(content).hexdigest(),
            "schema_fingerprint": schema_fingerprint(schema),
            "files": hashes,
            "audit": audit_gold_set(cases),
        },
    )
    return destination


def verify_generation(
    root: str | Path,
    *,
    reader: Callable[[str | Path], bytes | None] | None = None,
    require_knowledge: bool = False,
) -> list[str]:
    """Missing manifest or any changed/missing generated file invalidates the generation."""
    root = Path(root)
    frozen: dict[Path, bytes | None] = {}

    def read(path: Path) -> bytes | None:
        if path not in frozen:
            frozen[path] = reader(path) if reader else path.read_bytes()
        return frozen[path]

    try:
        manifest = json.loads(read(root / "gold-manifest.json") or b"null")
        required = set(EXPORT_PATHS.values())
        if require_knowledge:
            required.update(KNOWLEDGE_FILES)
        if (
            not isinstance(manifest, dict)
            or set(manifest)
            != {"schema_version", "canonical_sha256", "schema_fingerprint", "files", "audit"}
            or type(manifest["schema_version"]) is not int
            or manifest["schema_version"] != 1
            or not isinstance(manifest["files"], dict)
            or not isinstance(manifest["audit"], dict)
            or any(
                not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                for digest in [
                    manifest["canonical_sha256"],
                    manifest["schema_fingerprint"],
                    *manifest["files"].values(),
                ]
            )
            or not required <= set(manifest["files"])
            or not set(manifest["files"]) <= set(EXPORT_PATHS.values()) | set(KNOWLEDGE_FILES)
            or (
                set(manifest["files"]).intersection(KNOWLEDGE_FILES)
                and not set(KNOWLEDGE_FILES) <= set(manifest["files"])
            )
        ):
            return ["invalid Gold generation manifest"]
        errors: list[str] = []
        expected_views: dict[str, bytes] | None = None
        canonical = read(root / "gold-cases.jsonl")
        if canonical is None or sha256(canonical).hexdigest() != manifest["canonical_sha256"]:
            errors.append("changed or missing canonical Gold bytes")
        else:
            cases = load_gold_cases_bytes(canonical, source=root / "gold-cases.jsonl")
            if audit_gold_set(cases) != manifest["audit"]:
                errors.append("Gold generation audit does not match canonical cases")
            approved = [case for case in cases if case.status == "approved"]
            if not approved:
                errors.append("Gold generation requires reviewed approved final-purpose cases")
            if any(case.purpose not in EXPORT_PATHS for case in approved):
                errors.append("Gold generation contains a nonexportable approved purpose")
            if any(
                case.review is None
                or case.review.schema_fingerprint != manifest["schema_fingerprint"]
                for case in approved
            ):
                errors.append("approved review does not match the generation Schema")
            expected_views = _export_views(cases)
            errors.extend(_view_contract_errors(expected_views))
            errors.extend(isolation_errors(approved))
        contents: dict[str, bytes] = {}
        for relative, expected in manifest["files"].items():
            path = root / relative
            data = read(path)
            if data is None or sha256(data).hexdigest() != expected:
                errors.append(f"changed or missing Gold export: {relative}")
            if data is not None:
                contents[relative] = data
            if (
                expected_views is not None
                and relative in expected_views
                and data != expected_views[relative]
            ):
                errors.append(f"Gold export does not match approved canonical cases: {relative}")
        errors.extend(_view_contract_errors(contents))
        if set(manifest["files"]).intersection(KNOWLEDGE_FILES):
            errors.extend(_knowledge_contract_errors(contents, manifest["schema_fingerprint"]))
        return errors
    except (OSError, ValueError, KeyError, TypeError):
        return ["missing or malformed Gold generation manifest"]


def configured_generation_errors(
    paths: dict[str, str],
    knowledge_root: str,
    *,
    reader: Callable[[str | Path], bytes | None] | None = None,
) -> list[str]:
    root = Path(paths["test"]).resolve().parent.parent
    errors = verify_generation(root, reader=reader, require_knowledge=True)
    for split, value in paths.items():
        if Path(value).resolve() != (root / EXPORT_PATHS[split]).resolve():
            errors.append(f"{split}: all runtime datasets must belong to the same Gold generation")
    if Path(knowledge_root).resolve() != (root / "knowledge").resolve():
        errors.append("structured knowledge must belong to the same Gold generation")
    return errors


def assert_training_isolation(
    paths: dict[str, str],
    prompt_records: list[dict[str, Any]],
    *,
    negative_records: list[dict[str, Any]] | None = None,
    reader: Callable[[str | Path], bytes | None] | None = None,
) -> None:
    """Hold out Prompt, feedback and approved negatives from independent evaluation."""
    training: list[EvaluationCase] = []
    records = [(item, False) for item in prompt_records] + [
        (item, True) for item in negative_records or [] if item.get("status") == "approved"
    ]
    for index, (item, negative) in enumerate(records):
        sql = item.get("sql")
        fingerprint = None
        if isinstance(sql, str) and (sql.strip() or negative):
            fingerprint = (
                _negative_template_fingerprint(sql, item.get("error_types"))
                if negative
                else sql_template_fingerprint(sql)
            )
        training.append(
            EvaluationCase(
                str(item.get("id") or f"prompt-{index}"),
                "retrieval_train",
                str(
                    item.get("query_family_id")
                    or fingerprint
                    or item.get("id")
                    or f"prompt-{index}"
                ),
                fingerprint,
                str(item.get("question") or ""),
                item,
            )
        )

    def load(split: str) -> list[EvaluationCase]:
        path = paths[split]
        if reader is None:
            return load_evaluation_cases(path, expected_split=split)
        data = reader(path)
        if data is None:
            raise ValueError(f"missing frozen evaluation split: {split}")
        return load_evaluation_cases_bytes(data, source=path, expected_split=split)

    training.extend(load("retrieval_train"))
    groups = [training] + [load(split) for split in sorted(HELD_OUT)]
    assert_disjoint_splits(*groups)


def migrate_legacy_sources(
    workspace: str | Path, root: str | Path, *, quarantine: bool = False
) -> dict[str, Any]:
    """Preserve exact source bytes and import drafts; old status is never inherited.

    Quarantine is explicit and only empties the eight derived views after their
    original bytes have been archived and verified. No database files are touched.
    """
    workspace, root = Path(workspace).resolve(), Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    sources = EXPORT_PATHS | {"migration_review": "knowledge/examples/migration_review.jsonl"}
    snapshots: dict[str, bytes] = {}
    cases: list[GoldCase] = []
    for purpose, relative in sources.items():
        path = workspace / relative
        content = path.read_bytes()
        snapshots[relative] = content
        for line, record in enumerate(content.decode("utf-8").splitlines(), 1):
            if not record.strip():
                continue
            payload = json.loads(record)
            question = payload["question"]
            sql = payload.get("sql", payload.get("baseline_sql"))
            try:
                family = (
                    sql_template_fingerprint(sql) if isinstance(sql, str) and sql.strip() else ""
                )
            except ValueError:
                family = ""
            family = family or sha256(" ".join(question.casefold().split()).encode()).hexdigest()
            cases.append(
                GoldCase(
                    id=f"{purpose}:{payload.get('id', line)}",
                    question=question,
                    purpose=cast(Purpose, purpose),
                    query_family_id=f"family-{family}",
                    category=payload.get("category", purpose),
                    difficulty=payload.get("difficulty", "unknown"),
                    source=relative,
                    source_line=line,
                    source_sha256=sha256(content).hexdigest(),
                    status="candidate",
                    payload=payload,
                )
            )
        archive = root / "legacy" / relative
        atomic_bytes(archive, content)
        if archive.read_bytes() != content:
            raise ValueError(f"legacy archive verification failed: {relative}")
    for relative, content in snapshots.items():
        if (workspace / relative).read_bytes() != content:
            raise ValueError(f"legacy source changed during migration: {relative}")
    atomic_bytes(
        root / "cases.jsonl", "".join(case.model_dump_json() + "\n" for case in cases).encode()
    )
    manifest = {
        "schema_version": 1,
        "lifecycle": "candidate -> independent review -> executed evidence -> approved -> immutable export",
        "migration_policy": "all historical records require fresh review; previous status kept only in draft payload",
        "sources": {
            relative: sha256(content).hexdigest() for relative, content in snapshots.items()
        },
        "audit": audit_gold_set(cases),
    }
    atomic_json(root / "manifest.json", manifest)
    if quarantine:
        for relative in EXPORT_PATHS.values():
            atomic_bytes(workspace / relative, b"")
        source = workspace / "knowledge/schema/schema_snapshot.json"
        if source.exists():
            content = source.read_bytes()
            payload = json.loads(content)
            if validate_schema_contract(payload.get("tables")):
                target = source.with_name("legacy_schema_snapshot.json")
                if target.exists() and target.read_bytes() != content:
                    raise ValueError("refusing to replace a different legacy Schema archive")
                atomic_bytes(target, content)
                source.unlink()
    return manifest
