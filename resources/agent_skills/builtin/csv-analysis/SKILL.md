---
name: csv-analysis
description: Inspect CSV files for columns, quality issues, and summary metrics.
allowed-tools:
  - read_file
  - write_file
metadata:
  my_dify:
    version: 1.0.0
    execution_mode: inline
    scripts:
      - id: summarize_csv
        path: scripts/summarize_csv.py
        runtime: python3
        timeout_seconds: 30
        max_output_bytes: 1048576
        input_count: 1
        output_kind: json
---

When a CSV file is available, inspect its header, row count, missing values,
and obvious formatting issues before suggesting analysis steps.
