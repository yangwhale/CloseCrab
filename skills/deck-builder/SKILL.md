---
name: deck-builder
description: >-
  Build polished, on-brand slide decks (python-pptx → Google Slides) and richly
  formatted Google Docs, with a reusable design system, matplotlib charts,
  AI-generated imagery (Nano Banana), a render-verify loop, and link-stable
  Drive uploads (PATCH same fileId so the share link never changes). Use when
  the user wants to create or iterate a presentation / briefing deck / exec
  one-pager / formatted Google Doc — especially customer or executive briefings
  that go into Google Drive. Also covers extracting user-pasted images from the
  session transcript and converting decks/docs to PNG for visual self-check.
---

# Deck & Doc Builder

A battle-tested workflow for producing **beautiful, consistent slide decks and
Google Docs as code**, iterating fast, and shipping them straight into Google
Drive without the link ever changing.

The golden rule: **generate → render to image → look at it → fix → upload.**
Never claim a slide looks right without rendering it and viewing the PNG.

## When to use
- "Make/update a deck / PPT / briefing / one-pager" (customer, exec, internal).
- "Turn these notes / meeting minutes into a clean Google Doc."
- "Put this into our Drive folder" / "update the slides I shared earlier."
- Anything where layout quality + brand consistency matter.

## The loop (do this every iteration)
1. **Generate** the artifact with code (never hand-place by guessing):
   - Slides: a python-pptx script using `scripts/deck_helpers.py`.
   - Docs: rich HTML → uploaded as a Google Doc (see `reference.md`).
2. **Render to PNG and LOOK**: `scripts/render.sh deck.pptx 3 5` renders pages
   3–5. Read the PNGs. Check overflow, alignment, color, crops, fit.
3. **Fix** the code and re-render until it's right.
4. **Upload / update** to Drive with `scripts/gslides.py`:
   - First time: create (returns a fileId; **save it** to a dotfile).
   - Every later iteration: **PATCH the same fileId** → the share link is stable.
5. Render the *uploaded* result once more (export Slides/Doc → PDF → PNG) to
   confirm Google's conversion matches your intent.

Render is cheap and the only source of truth. A tight loop of small edits +
re-render beats trying to get it perfect blind.

## Slides: the design system (`scripts/deck_helpers.py`)
- 16:9 canvas `13.333" × 7.5"`, blank layout, everything absolutely positioned.
- Google palette (BLUE/RED/YELLOW/GREEN + grey ramp) as named constants.
- Core helpers (see file for signatures):
  - `txt(...)` — textbox; **`**bold**` markers inside the string toggle bold**,
    `\n` splits paragraphs. This is the workhorse.
  - `card(...)` rounded rectangle, `bar(...)` solid rect (also used as accent
    strips / thin connector lines), `hline(...)` connector, `dots(...)` the
    Google 4-dot motif, `pic(...)` image with **crop-to-fill** (no distortion),
    `video(...)` embedded mp4 with poster, `scrim(...)` dark alpha overlay,
    `title(...)` page title with optional 21:9 hero image + scrim, `notes(...)`.
- Patterns that read well: KPI cards row, horizontal bar chart drawn with
  `bar()`, alternating timeline, 2×N card grid, comparison table drawn with
  `txt`+`hline`, a colored callout band at the bottom for the "so what".
- Keep one slide = one idea. Put the takeaway in a colored band.

## Charts
- For **accurate data** (pie/donut/bars that must match a table), draw with
  **matplotlib** and embed the PNG — do not eyeball. Use a CJK font
  (`Noto Sans CJK`) for Chinese labels. Save as a **square** transparent PNG so
  `pic()` crop-to-fill doesn't clip outer labels. See `reference.md`.
- Make the chart numbers equal the table numbers. Inconsistent totals are the
  #1 thing reviewers catch.

## Imagery
- **Generate** decorative/concept images with Nano Banana 2
  (`gemini-3.1-flash-image` on Vertex). Prompt for clean, no-text, no-logo,
  brand-neutral visuals. Good for hero strips, concept diagrams, section art.
  Code in `reference.md`.
- **Real product/brand images** beat generated ones — pull from the user's
  Drive or the official site when authenticity matters.
- **User-pasted images** (screenshots they drop in chat) are NOT on disk as
  files — they're base64 in the session JSONL. Extract them with the snippet in
  `reference.md` (match by the `WxH` shown on the attachment chip). Don't say
  "I can't access the image"; go get it.

## Google Drive / Workspace (`scripts/gslides.py`)
- Auth: ADC at `~/.config/gcloud/application_default_credentials.json` must have
  **drive + presentations** scope; every request needs the
  `X-Goog-User-Project` header. (See "Setup" below.)
- **PPTX → Google Slides**: resumable upload with
  `mimeType: application/vnd.google-apps.presentation` (Drive auto-converts).
- **HTML → Google Doc**: same, `application/vnd.google-apps.document`. Google
  Docs import respects colored headings, `bgcolor` table cells, callout boxes
  (single-cell tables), and **base64 `data:` images** — verified. This is how
  you get a *pretty* Doc, not a plain one.
- **Link-stable iteration**: keep the fileId and **PATCH** it on every update
  (`uploadType=resumable`, `method=PATCH`). The URL stays the same so anyone you
  shared it with always sees the latest.
- **Export for self-check**: export the Slides/Doc to PDF, then PNG, and look.

## Content principles (for briefings)
- State outcomes plainly; verify before saying "done" (render it).
- Don't over-commit on the customer's behalf; ground vague claims in real,
  sourced numbers, and label estimates as estimates.
- Match the audience: internal vs customer-facing decks have different rules
  about citing sources, pricing, and roadmap specificity — confirm which.
- Strong logic > volume. Prefer a "problem → ask → how we help → decision"
  mapping table over long prose.

## Setup (first run / new user)
1. **Auth (ADC).** This skill uploads / creates / PATCHes files, so `drive` and
   `presentations` must be **FULL (write) scopes** — the `.readonly` variants
   cannot write. (No `documents` scope needed: Google **Docs are created/updated
   via Drive's HTML→Doc import**, which only needs `drive` write.)
   ```
   gcloud auth application-default login --scopes=openid,email,\
   https://www.googleapis.com/auth/cloud-platform,\
   https://www.googleapis.com/auth/drive,\
   https://www.googleapis.com/auth/presentations
   ```
   `drive`(write) creates/updates both Slides and Docs; `presentations`(write)
   is for any Slides-API edits; `cloud-platform` covers Vertex (Nano Banana) and
   Slides/Docs API reads.
2. **Project** defaults to `cloud-llm-preview1` for both Drive calls
   (`X_GOOG_USER_PROJECT` in `gslides.py`) and Nano Banana (Vertex `PROJECT` in
   `reference.md`). If you can access that project, change nothing; else set yours.
3. `pip install python-pptx pillow matplotlib`; have `libreoffice` + `poppler`
   (`pdftoppm`, `pdfinfo`) on PATH for the render loop.

## Files in this skill
- `scripts/deck_helpers.py` — the python-pptx design-system library (import it).
- `scripts/gslides.py` — Workspace: token, upload→Slides/Doc, PATCH, export PDF.
- `scripts/render.sh` — pptx/pdf → PNG pages for visual self-check.
- `reference.md` — Nano Banana gen, matplotlib donut, pasted-image extraction,
  Google-Doc HTML styling recipes, gotchas.
