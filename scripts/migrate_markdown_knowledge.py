"""One-time migration from a legacy question Markdown file to review candidates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

from sqlglot import exp, parse_one


EXAMPLE_RE = re.compile(
    r"^###\s+问题\d*[:：](?P<question>.+?)\s*$.*?"
    r"```sql\s*(?P<sql>.+?)```",
    re.MULTILINE | re.DOTALL,
)


def migrate(question_path: Path, output_path: Path) -> int:
    content = question_path.read_text(encoding="utf-8")
    training_section = content.split("## 5. 训练示例", 1)[-1].split("## 6.", 1)[0]
    records = []
    seen: set[tuple[str, str]] = set()
    for index, match in enumerate(EXAMPLE_RE.finditer(training_section), 1):
        question = match.group("question").strip()
        sql = match.group("sql").strip()
        tree = parse_one(sql, read="tsql")
        tables = list(dict.fromkeys(table.name for table in tree.find_all(exp.Table)))
        key = (" ".join(question.split()).lower(), tree.sql(dialect="tsql"))
        if key in seen:
            continue
        seen.add(key)
        records.append(
            {
                "id": f"legacy-question-{index:03d}",
                "question": question,
                "sql": sql,
                "tables": tables,
                "status": "candidate",
                "source": str(question_path),
                "review_note": "迁移样本，需通过 Schema/AST/执行结果审核后改为 approved",
            }
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records),
        encoding="utf-8",
    )
    return len(records)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--question",
        required=True,
        help="legacy Markdown file containing question/SQL examples",
    )
    parser.add_argument(
        "--output", default="knowledge/examples/migration_review.jsonl"
    )
    args = parser.parse_args()
    count = migrate(Path(args.question), Path(args.output))
    print(f"migrated {count} candidate examples to {args.output}")


if __name__ == "__main__":
    main()
