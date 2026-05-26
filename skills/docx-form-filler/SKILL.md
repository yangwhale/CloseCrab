---
name: docx-form-filler
description: Fill a .docx template (forms, portfolios, application sheets, report cards) with data extracted from a source document (PDF, another docx, chat input) while preserving the template's formatting. Use when the user provides a .docx template plus a data source and says "帮我填这个表"、"按这个 PDF 填模板"、"把这份资料填进 docx"、"fill this form"、"fill this template from a PDF"、"portfolio 填表"、"申请表填一下". Handles paragraphs, table cells, AND textbox/cover-page content (which python-docx alone cannot reach).
---

# docx-form-filler

## When this skill is the right tool

User has:
- A `.docx` **template** with blank fields (underscores, empty table rows, placeholder examples like "例1")
- A **data source** (PDF / another doc / chat content) containing the real values
- Wants the template filled while keeping its fonts, table borders, layout intact

For old `.doc` (binary), tell the user to convert to `.docx` first (Word → Save As).

## Core workflow

### 1. Inspect the template structure FIRST

```bash
python3 scripts/inspect_docx.py /path/to/template.docx
```

This prints every non-empty paragraph (with index `P<n>`) and every table (with `R<row>` cells). Note:
- Which **paragraph indices** hold the blanks (look for underscores `____`, 〔中文〕/〔English〕 markers)
- Each **table's column layout** — column index of Year / Items / Award / Position / etc.
- Header rows to skip (usually rows 0–1)
- "Example" placeholder columns to clear (e.g. "例1".."例5" in the rightmost column)

### 2. Extract source data

For PDF sources:
```bash
pip install pymupdf   # if missing
python3 scripts/extract_pdf.py /path/to/source.pdf
```

Read it carefully. Map each piece of data to the template field it belongs in.

### 3. Plan before writing — confirm with user

Before running the fill script, **tell the user**:
- What you'll put in each non-trivial field
- Any **missing data** (parent names, addresses, etc.) — ask, don't invent
- Any **ambiguous** mapping (e.g. "Primary Level: 5 or 6?" when student is P.5 applying to P.6)
- Your picks when the source has more achievements than table slots (curate the strongest 5)

This step matters — wrong filling is worse than asking one extra question.

### 4. Write the fill script

Copy `scripts/fill_template_example.py`, edit `SRC` / `DST` and the data sections. Always **write to a new path** — never overwrite the original template.

Key APIs (from `scripts/fill_helpers.py`):

```python
from fill_helpers import set_para_text, set_cell_text, xml_replace

# Paragraph: replaces text but keeps first run's font/size/color
set_para_text(doc.paragraphs[59], "Name：張三〔中文〕")

# Table cell: supports multi-line via \n, removes extra paragraphs
set_cell_text(row.cells[2], "P.5 First Term\nTop 3 of form")

# Textbox / cover / SDT content (python-docx CANNOT reach these)
xml_replace(output_path, {"OLD PLACEHOLDER": "New Name"})
```

### 5. Verify

Run `inspect_docx.py` on the **output** file and visually scan every filled row. Then `open` it in Word and eyeball the layout.

## Critical gotchas

### Textbox / cover-page content needs xml_replace

If the template has a cover page like "Portfolio of <Name>" and python-docx's `doc.paragraphs` iteration **doesn't find** the placeholder text, it's inside a `<w:txbxContent>` (textbox / DrawingML shape). `python-docx` ignores these.

Workflow:
1. `grep -rl "PlaceholderName" /tmp/unzipped_docx/` to confirm it's in `word/document.xml` but not reachable via paragraphs
2. Use `xml_replace(path, {"PlaceholderName": "RealName"})` after `doc.save()`

### Preserve formatting — never use `cell.text = "x"` or `p.text = "x"`

The python-docx setter `p.text = "..."` nukes all runs and creates a new one with **default** formatting — losing the template's careful font/color. Always use the `set_para_text` / `set_cell_text` helpers which keep the first run's formatting.

### Empty rows: leave them empty, don't delete

Templates often have extra blank rows (e.g. rows numbered 6/7/8/9 after 5 example rows). **Leave them as-is** — don't delete or fill with garbage. They're intentional spare slots.

### Clear placeholder example columns

If a column contains "例1"、"例2"、"Sample 1" etc., explicitly clear it:
```python
set_cell_text(row.cells[5], "")
```

### Year/date cells may be split run-by-run

Inspecting may show a cell's text as `"2018- | 2017"` (where ` | ` is paragraph break) or year split into separate runs `"20" "1" "8"`. `set_cell_text` collapses all of this into a single clean cell — that's the desired behavior.

### Don't invent missing data

If the source PDF has no parent names, no address Chinese translation, etc. — **ask the user** rather than guessing. Filling a school application form with wrong info is high-stakes.

## Typical session shape

```
1. inspect_docx.py template.docx                  → understand layout
2. extract_pdf.py source.pdf                      → gather data
3. (tell user plan, ask about missing/ambiguous)  → confirm
4. write fill script using fill_helpers           → execute
5. inspect_docx.py output.docx                    → verify text
6. xml_replace for any textbox content            → fix cover
7. open output.docx in Word                       → visual check
```
