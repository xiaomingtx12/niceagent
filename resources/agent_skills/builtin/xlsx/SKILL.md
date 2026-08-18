---
name: xlsx
description: Analyze, create, and update Excel workbooks with reliable formulas, formatting, data validation, and clear summary outputs.
allowed-tools:
  - read_file
  - write_file
  - execute
metadata:
  my_dify:
    version: 1.0.0
    category: data
    requires:
      - openpyxl
      - pandas
    execution_mode: sandbox
---

# Spreadsheet Analysis

Use this skill for `.xlsx` inspection, data cleaning, reporting, formula
updates, workbook generation, or chart-ready summaries.

## Workflow

1. Inspect workbook sheets, dimensions, headers, merged cells, formulas,
   hidden sheets, named ranges, and data types before changing anything.
2. Preserve the original workbook unless the user explicitly requests an
   in-place update. Write derived outputs to `/artifacts`.
3. Normalize data carefully. Distinguish blank, zero, invalid, and missing
   values; report inferred types and assumptions.
4. Keep formulas as formulas when the workbook is intended for further use.
   When calculating values externally, state that recalculation may require
   Excel or another compatible engine.
5. Validate totals, row counts, duplicate handling, date ranges, and output
   sheet names after writing.
6. For charts or formatted reports, use stable column widths, readable number
   formats, freeze panes, filters, and a concise summary sheet.

Never overwrite source data silently and never present a computed result
without explaining the filtering and aggregation rules.
