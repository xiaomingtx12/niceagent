---
name: docx
description: Create, read, edit, and inspect professional Word documents with structured headings, tables, styles, and reliable output validation.
allowed-tools:
  - read_file
  - write_file
  - execute
metadata:
  my_dify:
    version: 1.0.0
    category: documents
    requires:
      - python-docx
    execution_mode: sandbox
---

# Word Documents

Use this skill for `.docx` creation, extraction, editing, restructuring, or
formatting tasks.

## Workflow

1. Inspect the source document before editing. Record its headings, tables,
   headers, footers, page breaks, and important styles.
2. Use `python-docx` for structured edits. Preserve existing styles and
   numbering whenever possible instead of rebuilding the document as plain
   text.
3. Keep document structure semantic: title, headings, paragraphs, lists,
   tables, captions, and references should use the appropriate Word styles.
4. For replacements, update only the intended text and preserve surrounding
   formatting, links, tables, and section layout.
5. After writing, reopen the document and inspect paragraphs, tables, and
   section properties. Report limitations for tracked changes, comments,
   complex fields, or unsupported embedded objects.
6. Save final files under `/workspace` or `/artifacts` and give the exact
   output path.

Do not claim that visual formatting is correct without inspecting the resulting
document or rendering it when that capability is available.
