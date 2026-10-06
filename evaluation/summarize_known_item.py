from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Mapping


def parse_rank(value: str) -> int | None:
    """Return an integer rank, or None when the item was outside the pool."""
    cleaned = value.strip()
    if not cleaned or cleaned.startswith(">"):
        return None
    return int(cleaned)


def metric_block(ranks: Iterable[int | None]) -> dict[str, float | int]:
    values = list(ranks)
    if not values:
        return {"n": 0, "hit_at_1": 0.0, "hit_at_3": 0.0, "hit_at_10": 0.0, "mrr": 0.0}

    size = len(values)
    return {
        "n": size,
        "hit_at_1": sum(rank is not None and rank <= 1 for rank in values) / size,
        "hit_at_3": sum(rank is not None and rank <= 3 for rank in values) / size,
        "hit_at_10": sum(rank is not None and rank <= 10 for rank in values) / size,
        "mrr": sum(1 / rank if rank else 0 for rank in values) / size,
    }


def summarize_rows(rows: Iterable[Mapping[str, str]]) -> dict[str, object]:
    materialized = list(rows)
    grouped: dict[str, list[int | None]] = defaultdict(list)
    all_ranks: list[int | None] = []

    for row in materialized:
        disease = row["질병구분"].strip()
        rank = parse_rank(row["원본 순위"])
        grouped[disease].append(rank)
        all_ranks.append(rank)

    return {
        "protocol": "known-item retrieval",
        "candidate_pool_per_query": 120,
        "overall": metric_block(all_ranks),
        "by_disease": {name: metric_block(ranks) for name, ranks in grouped.items()},
    }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Aggregate GoldenTime known-item retrieval results.")
    parser.add_argument("csv_path", type=Path, help="Row-level CSV produced by verify_known_item.py")
    parser.add_argument("--output", type=Path, help="Optional JSON output path")
    args = parser.parse_args()

    with args.csv_path.open(encoding="utf-8-sig", newline="") as handle:
        summary = summarize_rows(csv.DictReader(handle))

    summary["source_sha256"] = sha256(args.csv_path)
    rendered = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
