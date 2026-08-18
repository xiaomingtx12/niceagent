"""Summarize one CSV through the builtin Skill request-file contract."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


def load_request(path: str) -> dict[str, Any]:
    """Load the server-generated immutable request document."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def summarize_csv(
    path: str,
    *,
    delimiter: str,
    max_rows: int,
) -> dict[str, Any]:
    """Compute bounded row and missing-value statistics."""
    with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, delimiter=delimiter)
        columns = reader.fieldnames or []
        missing = {column: 0 for column in columns}
        row_count = 0
        truncated = False
        for row in reader:
            if row_count >= max_rows:
                truncated = True
                break
            row_count += 1
            for column in columns:
                if row.get(column) in (None, ""):
                    missing[column] += 1
    return {
        "columns": columns,
        "row_count": row_count,
        "missing_values": missing,
        "truncated": truncated,
    }


def main() -> None:
    """Read the fixed request path and write the declared JSON output."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    request = load_request(parser.parse_args().request)
    arguments = request.get("arguments", {})
    result = summarize_csv(
        request["inputs"][0],
        delimiter=arguments.get("delimiter", ","),
        max_rows=arguments.get("max_rows", 10000),
    )
    Path(request["output"]).write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
