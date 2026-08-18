---
name: pdf
description: Read, extract, summarize, inspect, transform, and generate PDF documents while preserving page structure and reporting limitations clearly.
allowed-tools:
  - read_file
  - write_file
  - execute
metadata:
  my_dify:
    version: 1.0.0
    category: documents
    requires:
      - pypdf
      - pdfminer.six
    execution_mode: sandbox
---

# PDF Processing

Use this skill for PDF extraction, summarization, page inspection, merging,
splitting, metadata inspection, or PDF generation.

## Workflow

1. Identify whether the PDF is text-based, image-only, encrypted, or contains
   forms/tables before choosing an extraction method.
2. For text PDFs, use `pypdf` or `pdfminer.six`. Preserve page boundaries and
   page numbers in extracted notes.
3. For scanned pages, report that OCR or image processing is required. Do not
   silently invent text that could not be extracted.
4. For tables, validate row and column alignment against the rendered page
   before exporting structured data.
5. When editing or generating a PDF, verify page size, rotation, fonts,
   clipping, hyperlinks, and output file existence.
6. Keep intermediate files under `/workspace` or `/artifacts`, and explain
   which output is the final deliverable.

Always distinguish extracted text from interpretation. Mention pages that were
unreadable, encrypted, truncated, or only partially processed.
