---
name: csv-analysis
description: Inspect CSV files for columns, quality issues, and summary metrics.
allowed-tools:
  - read_file
  - write_file
  - execute
metadata:
  my_dify:
    version: 1.0.0
---

When a CSV file is available, inspect its header, row count, missing values,
and obvious formatting issues before suggesting analysis steps.
