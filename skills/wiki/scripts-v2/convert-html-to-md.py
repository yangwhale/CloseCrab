#!/usr/bin/env python3
"""Convert CC Wiki HTML pages to Quartz-compatible Markdown with frontmatter.

Usage:
    # Convert all pages
    python3 convert-html-to-md.py

    # Convert specific pages
    python3 convert-html-to-md.py sources/tpu-v7.html entities/jax.html

    # Dry run (show what would be converted)
    python3 convert-html-to-md.py --dry-run
"""

import os
import re
import sys
import glob
import argparse
from pathlib import Path
from bs4 import BeautifulSoup, NavigableString, Tag
from markdownify import MarkdownConverter

WIKI_DIR = Path.home() / "my-wiki" / "wiki"
OUTPUT_DIR = Path.home() / "my-wiki-v2" / "content"
SUBDIRS = ["sources", "entities", "concepts", "analyses"]


_BOX_RE = re.compile(r'[┌┐└┘├┤┬┴┼─│▼▲►◄═╔╗╚╝╠╣╦╩╬╭╮╰╯]')


class WikiMarkdownConverter(MarkdownConverter):
    """Custom converter that turns wiki-link <a> tags into [[wikilinks]]."""

    def _has_class(self, el, *targets):
        """Check if element has any of the target classes."""
        classes = el.get("class", [])
        if isinstance(classes, str):
            classes = classes.split()
        return any(c in classes for c in targets)

    def _get_classes(self, el):
        """Get element classes as a list."""
        classes = el.get("class", [])
        if isinstance(classes, str):
            classes = classes.split()
        return classes

    def convert_div(self, el, text, parent_tags):
        """Handle custom div types: diagrams, stat cards, charts, callouts, etc."""
        classes = self._get_classes(el)

        # --- Info grid / stat card grids → compact inline format ---
        if any(c in classes for c in ["summary-grid", "grid", "stats-grid", "info-grid"]):
            cards = el.find_all("div", class_=lambda x: x and any(
                c in (x if isinstance(x, list) else x.split())
                for c in ["summary-card", "card", "stat-card"]
            ))
            if cards:
                parts = []
                for card in cards:
                    value_el = card.find("div", class_=lambda x: x and "value" in (x if isinstance(x, list) else x.split()))
                    label_el = card.find("div", class_=lambda x: x and "label" in (x if isinstance(x, list) else x.split()))
                    if not label_el:
                        label_el = card.find("div", class_=lambda x: x and "title" in (x if isinstance(x, list) else x.split()))
                    val = value_el.get_text(strip=True) if value_el else ""
                    lbl = label_el.get_text(strip=True) if label_el else ""
                    if val and lbl:
                        parts.append(f"**{val}** {lbl}")
                if parts:
                    return '\n\n' + ' · '.join(parts) + '\n\n'

        # --- Spec grids → table ---
        if "specs" in classes:
            specs = el.find_all("div", class_="spec")
            if specs:
                rows = []
                for spec in specs:
                    lbl = spec.find("div", class_="spec-label")
                    val = spec.find("div", class_="spec-value")
                    det = spec.find("div", class_="spec-detail")
                    lbl_t = lbl.get_text(strip=True) if lbl else ""
                    val_t = val.get_text(strip=True) if val else ""
                    det_t = det.get_text(strip=True) if det else ""
                    detail = f" ({det_t})" if det_t else ""
                    rows.append(f"| {lbl_t} | **{val_t}**{detail} |")
                header = "| 参数 | 值 |\n|---|---|"
                return f"\n{header}\n" + "\n".join(rows) + "\n"

        # --- Callout boxes → blockquote ---
        if "callout" in classes:
            # Callout with num/unit (stat callout) → inline stat
            num_el = el.find("div", class_="num")
            unit_el = el.find("div", class_="unit")
            if num_el and unit_el:
                num = num_el.get_text(strip=True)
                unit = unit_el.get_text(" ", strip=True)
                return f'\n\n**{num}** {unit}\n\n'
            # Regular callout → blockquote
            return f'\n\n> {text.strip()}\n\n'

        # --- Insight boxes → blockquote ---
        if "insight" in classes or "insight-box" in classes:
            title_el = el.find("div", class_="insight-title")
            title = title_el.get_text(strip=True) if title_el else ""
            # Get content excluding title
            body = text.strip()
            if title and body.startswith(title):
                body = body[len(title):].strip()
            if title:
                return f'\n\n> **{title}**\n> {body}\n\n'
            return f'\n\n> {body}\n\n'

        # --- Note boxes → blockquote ---
        if "note" in classes:
            return f'\n\n> {text.strip()}\n\n'

        # --- Card with title → section ---
        if "card" in classes:
            # Card with card-label/card-value (benchmark style)
            card_lbl = el.find("div", class_="card-label")
            card_val = el.find("div", class_="card-value")
            if card_lbl and card_val:
                lbl = card_lbl.get_text(strip=True)
                val = card_val.get_text(strip=True)
                note_el = el.find("div", class_="card-note")
                note = f" ({note_el.get_text(strip=True)})" if note_el else ""
                return f'\n**{val}** {lbl}{note}\n'
            # Card with title
            title_el = el.find("div", class_=lambda x: x and any(
                c in (x if isinstance(x, list) else x.split())
                for c in ["card-title", "card-header"]
            ))
            if title_el:
                title = title_el.get_text(strip=True)
                body = text.strip()
                if body.startswith(title):
                    body = body[len(title):].strip()
                return f'\n\n**{title}**\n\n{body}\n\n'
            return f'\n{text}\n'

        # --- Summary box (label/value/unit) → inline stat ---
        if "summary-box" in classes:
            label_el = el.find("div", class_="label")
            value_el = el.find("div", class_="value")
            unit_el = el.find("div", class_="unit")
            lbl = label_el.get_text(strip=True) if label_el else ""
            val = value_el.get_text(strip=True) if value_el else ""
            unit = unit_el.get_text(strip=True) if unit_el else ""
            if val:
                parts = [f"**{val}**"]
                if unit:
                    parts.append(unit)
                if lbl:
                    parts.append(f"({lbl})")
                return '\n\n' + ' '.join(parts) + '\n\n'

        # --- GPU row → table ---
        if "gpu-row" in classes:
            cards = el.find_all("div", class_="gpu-card")
            if cards:
                headers, ranges, counts = [], [], []
                for card in cards:
                    name_el = card.find("div", class_="gpu-name")
                    range_el = card.find("div", class_="expert-range")
                    count_el = card.find("div", class_="expert-count")
                    headers.append(name_el.get_text(strip=True) if name_el else "")
                    ranges.append(range_el.get_text(strip=True) if range_el else "")
                    counts.append(count_el.get_text(strip=True) if count_el else "")
                h = "| " + " | ".join(headers) + " |"
                s = "|" + "|".join(["---"] * len(headers)) + "|"
                rows = []
                if any(ranges):
                    rows.append("| " + " | ".join(ranges) + " |")
                if any(counts):
                    rows.append("| " + " | ".join(counts) + " |")
                return f"\n{h}\n{s}\n" + "\n".join(rows) + "\n"
            return f'\n{text}\n'

        # --- GPU box (diagram element) → compact text ---
        if "gpu-box" in classes:
            label = el.find("div", class_="gpu-label")
            blocks = el.find_all("div", class_=lambda x: x and "data-block" in (x if isinstance(x, list) else x.split()))
            parts = []
            if label:
                parts.append(f"**{label.get_text(strip=True)}**")
            for b in blocks:
                parts.append(b.get_text(strip=True))
            if parts:
                return '\n' + ': '.join(parts) + '\n'
            return f'\n{text}\n'

        # --- Diagram containers → code block ---
        if "diagram" in classes:
            raw = el.get_text()
            return f'\n```\n{raw.strip()}\n```\n'

        # --- Compare columns → table ---
        if "compare" in classes:
            cols = el.find_all("div", class_="compare-col")
            if not cols:
                cols = el.find_all("div", class_="compare-card")
            if len(cols) >= 2:
                headers = []
                bodies = []
                for col in cols:
                    h_el = col.find(["h3", "h4", "div"], class_=lambda x: x and any(
                        c in (x if isinstance(x, list) else x.split())
                        for c in ["title", "card-title", "compare-title"]
                    ))
                    if not h_el:
                        h_el = col.find(["h3", "h4"])
                    headers.append(h_el.get_text(strip=True) if h_el else "")
                    # Get body text
                    body_text = col.get_text("\n", strip=True)
                    if h_el:
                        h_text = h_el.get_text(strip=True)
                        body_text = body_text.replace(h_text, "", 1).strip()
                    bodies.append(body_text.replace("\n", " ")[:200])
                h = "| " + " | ".join(headers) + " |"
                s = "|" + "|".join(["---"] * len(headers)) + "|"
                r = "| " + " | ".join(bodies) + " |"
                return f"\n{h}\n{s}\n{r}\n"

        # --- Info rows → table ---
        if "info-row" in classes:
            lbl = el.find("div", class_="info-label")
            val = el.find("div", class_="info-value")
            if lbl and val:
                return f"| {lbl.get_text(strip=True)} | {val.get_text(strip=True)} |\n"

        # --- Params (tag list) → inline ---
        if "params" in classes:
            tags = el.find_all("span", class_="tag")
            if tags:
                parts = [t.get_text(strip=True) for t in tags]
                return '\n\n' + ' · '.join(parts) + '\n\n'

        # --- Timeline → list ---
        if "timeline" in classes:
            items = el.find_all("div", class_="timeline-item")
            if items:
                lines = []
                for item in items:
                    date_el = item.find("div", class_=lambda x: x and any(
                        c in (x if isinstance(x, list) else x.split())
                        for c in ["timeline-date", "timeline-title"]
                    ))
                    desc_el = item.find("div", class_="timeline-desc")
                    if not desc_el:
                        desc_el = item.find("div", class_="timeline-content")
                    date = date_el.get_text(strip=True) if date_el else ""
                    desc = desc_el.get_text(" ", strip=True) if desc_el else item.get_text(" ", strip=True)
                    if date:
                        lines.append(f"- **{date}**: {desc}")
                    else:
                        lines.append(f"- {desc}")
                return '\n\n' + '\n'.join(lines) + '\n\n'

        # --- Step boxes → bold title + content ---
        if "step-box" in classes or "step-content" in classes:
            title_el = el.find("div", class_="step-title")
            if title_el:
                title = title_el.get_text(strip=True)
                body = text.strip()
                if body.startswith(title):
                    body = body[len(title):].strip()
                return f'\n\n**{title}**\n\n{body}\n\n'
            return f'\n{text}\n'

        # --- Step (inline with step-num) → list item ---
        if "step" in classes and el.find("span", class_="step-num"):
            num_el = el.find("span", class_="step-num")
            num = num_el.get_text(strip=True)
            rest = el.get_text(" ", strip=True)
            if rest.startswith(num):
                rest = rest[len(num):].strip()
            return f'\n- **{num}**: {rest}\n'

        # --- Metric rows → compact ---
        if "metric-row" in classes or "metric" in classes:
            # Try multiple selector strategies
            label_el = (
                el.find(["span", "div"], class_="metric-label") or
                el.find(["span", "div"], class_=lambda x: x and "label" in (x if isinstance(x, list) else x.split()))
            )
            value_el = (
                el.find(["span", "div"], class_="metric-value") or
                el.find(["span", "div"], class_=lambda x: x and "value" in (x if isinstance(x, list) else x.split()))
            )
            if label_el and value_el:
                lbl = label_el.get_text(strip=True)
                val = value_el.get_text(strip=True)
                sub_el = el.find(["div", "span"], class_="metric-sub")
                sub = f" ({sub_el.get_text(strip=True)})" if sub_el else ""
                return f'\n- **{lbl}**: {val}{sub}\n'

        # --- Badge row → inline badges ---
        if "badge-row" in classes:
            badges = el.find_all("span", class_=lambda x: x and "badge" in (x if isinstance(x, list) else x.split()))
            if badges:
                parts = [b.get_text(strip=True) for b in badges]
                return '\n\n' + ' · '.join(parts) + '\n\n'

        # --- Metric card/box → compact ---
        if any(c in classes for c in ["metric-card", "metric-box"]):
            label_el = el.find("div", class_=lambda x: x and "label" in (x if isinstance(x, list) else x.split()))
            value_el = el.find("div", class_=lambda x: x and "value" in (x if isinstance(x, list) else x.split()))
            if label_el and value_el:
                return f'\n**{value_el.get_text(strip=True)}** {label_el.get_text(strip=True)}\n'

        # --- Info card (label/value/detail) → inline stat ---
        if "info-card" in classes:
            label_el = el.find("div", class_="label")
            value_el = el.find("div", class_="value")
            detail_el = el.find("div", class_="detail")
            lbl = label_el.get_text(strip=True) if label_el else ""
            val = value_el.get_text(strip=True) if value_el else ""
            det = detail_el.get_text(strip=True) if detail_el else ""
            detail = f" ({det})" if det else ""
            if val and lbl:
                return f'\n**{val}** {lbl}{detail}\n'

        # --- Highlight card → inline stat ---
        if "highlight-card" in classes:
            value_el = el.find("div", class_="value")
            label_el = el.find("div", class_="label")
            compare_el = el.find("div", class_="compare")
            val = value_el.get_text(strip=True) if value_el else ""
            lbl = label_el.get_text(strip=True) if label_el else ""
            cmp = compare_el.get_text(" ", strip=True) if compare_el else ""
            parts = [f"**{val}**", lbl]
            if cmp:
                parts.append(f"({cmp})")
            return '\n' + ' '.join(parts) + '\n'

        # --- Card with card-label/card-value (benchmark cards) → inline stat ---
        if "card-label" in classes or "card-value" in classes:
            return f' {el.get_text(strip=True)} '

        # --- Alert boxes → blockquote ---
        if "alert" in classes:
            return f'\n\n> {text.strip()}\n\n'

        # --- Finding card → numbered finding ---
        if "finding-card" in classes:
            num_el = el.find("span", class_="num")
            num = num_el.get_text(strip=True) if num_el else ""
            body = text.strip()
            if num and body.startswith(num):
                body = body[len(num):].strip()
            prefix = f"**{num}.** " if num else ""
            return f'\n\n{prefix}{body}\n\n'

        # --- Step card → section ---
        if "step-card" in classes:
            return f'\n{text}\n'

        # --- Stat card (standalone) → inline ---
        if "stat-card" in classes or "stat-box" in classes or "stat" in classes:
            label_el = el.find("div", class_=lambda x: x and any(
                c in (x if isinstance(x, list) else x.split())
                for c in ["stat-label", "label"]
            ))
            value_el = el.find("div", class_=lambda x: x and any(
                c in (x if isinstance(x, list) else x.split())
                for c in ["stat-value", "value"]
            ))
            if label_el and value_el:
                return f'\n**{value_el.get_text(strip=True)}** {label_el.get_text(strip=True)}\n'

        # --- Tool card → section ---
        if "tool-card" in classes:
            header_el = el.find("div", class_="tool-header")
            body_el = el.find("div", class_="tool-body")
            if header_el:
                title = header_el.get_text(strip=True)
                body = body_el.get_text(" ", strip=True) if body_el else ""
                return f'\n\n**{title}**\n\n{body}\n\n'

        # --- Journey grid → table ---
        if "journey-grid" in classes:
            labels = el.find_all("div", class_="journey-label")
            contents = el.find_all("div", class_="journey-content")
            if labels:
                lines = []
                for i, lbl in enumerate(labels):
                    lbl_text = lbl.get_text(" ", strip=True)
                    content_text = contents[i].get_text(" ", strip=True) if i < len(contents) else ""
                    lines.append(f"- **{lbl_text}**: {content_text}")
                return '\n\n' + '\n'.join(lines) + '\n\n'

        # --- Grid containers (grid2, grid3, grid-2, card-grid) → pass through ---
        if any(c in classes for c in ["grid2", "grid3", "grid-2", "card-grid", "grid-3"]):
            return f'\n{text}\n'

        # --- Bar chart → horizontal table ---
        if "bar-chart" in classes:
            groups = el.find_all("div", class_="bar-group")
            if groups:
                labels, values = [], []
                for g in groups:
                    val_el = g.find("div", class_=lambda x: x and "bar-value" in (x if isinstance(x, list) else x.split()))
                    lbl_el = g.find("div", class_="bar-label")
                    val = val_el.get_text(strip=True) if val_el else ""
                    lbl = lbl_el.get_text(" ", strip=True) if lbl_el else ""
                    labels.append(lbl)
                    values.append(val)
                if labels:
                    header = "| " + " | ".join(labels) + " |"
                    sep = "|" + "|".join(["---"] * len(labels)) + "|"
                    row = "| " + " | ".join(f"**{v}**" for v in values) + " |"
                    return f"\n{header}\n{sep}\n{row}\n"
            return f'\n{text}\n'

        # --- Bar row (horizontal bar chart variant) → list ---
        if "bar-row" in classes:
            label_el = el.find("div", class_="bar-label")
            value_el = el.find("div", class_="bar-value")
            if label_el and value_el:
                return f'\n- {label_el.get_text(strip=True)}: **{value_el.get_text(strip=True)}**\n'

        # --- Known diagram classes → code block ---
        if any(c in classes for c in ["arch-diagram", "tree", "architecture-diagram", "layer-diagram"]):
            raw = el.get_text()
            return f'\n```\n{raw.strip()}\n```\n'

        # --- Chart container → just pass through ---
        if "chart-container" in classes:
            return f'\n{text}\n'

        # --- Code block / code panel → code block ---
        if any(c in classes for c in ["code-block", "code-panel"]):
            raw = el.get_text()
            return f'\n```\n{raw.strip()}\n```\n'

        # --- Section containers → pass through with title ---
        if "section" in classes:
            title_el = el.find("div", class_="section-title")
            if title_el:
                title = title_el.get_text(strip=True)
                body = text.strip()
                if body.startswith(title):
                    body = body[len(title):].strip()
                return f'\n\n### {title}\n\n{body}\n\n'
            return f'\n{text}\n'

        # --- SVG containers → placeholder ---
        if "svg-container" in classes:
            return '\n*(diagram)*\n'

        # --- Table wrapper → pass through ---
        if "table-wrapper" in classes:
            return f'\n{text}\n'

        # --- PR item → list item ---
        if "pr-item" in classes:
            title_el = el.find("div", class_="pr-title")
            author_el = el.find("div", class_="pr-author")
            title = title_el.get_text(strip=True) if title_el else ""
            author = author_el.get_text(strip=True) if author_el else ""
            if title:
                suffix = f" ({author})" if author else ""
                return f'\n- {title}{suffix}\n'

        # --- Data block → inline ---
        if "data-block" in classes:
            return f' `{el.get_text(strip=True)}` '

        # --- Arch box / flow box → bold label ---
        if any(c in classes for c in ["arch-box", "flow-box", "flow-step"]):
            return f' **{el.get_text(strip=True)}** '

        # --- Rating item → list ---
        if "rating-item" in classes:
            return f'\n- {el.get_text(" ", strip=True)}\n'

        # --- Phase → section ---
        if "phase" in classes:
            return f'\n\n{text}\n\n'

        # --- Finding / issue / solution → blockquote items ---
        if any(c in classes for c in ["finding", "issue", "solution", "problem", "reason"]):
            return f'\n\n> {text.strip()}\n\n'

        # --- Highlight → bold ---
        if "highlight" in classes and not el.find("div"):
            return f'\n\n**{el.get_text(strip=True)}**\n\n'

        # --- Config block → code block ---
        if "config-block" in classes:
            header_el = el.find("div", class_="config-header")
            header = header_el.get_text(strip=True) if header_el else ""
            lines_el = el.find_all("div", class_="line")
            if lines_el:
                code_lines = []
                for line_div in lines_el:
                    key_el = line_div.find("span", class_="key")
                    val_el = line_div.find("span", class_="val")
                    if key_el:
                        key = key_el.get_text(strip=True)
                        val = val_el.get_text(strip=True) if val_el else ""
                        code_lines.append(f"{key} {val}")
                    else:
                        code_lines.append(line_div.get_text(strip=True))
                title = f"**{header}**\n\n" if header else ""
                return f'\n\n{title}```\n' + '\n'.join(code_lines) + '\n```\n'

        # --- Line (config line) → skip if parent is config-block ---
        if "line" in classes and el.parent and self._has_class(el.parent, "config-body", "config-block"):
            return ''  # Handled by config-block

        # --- MoE step → list item ---
        if "moe-step" in classes:
            icon_el = el.find("div", class_="icon")
            label_el = el.find("div", class_="label")
            desc_el = el.find("div", class_="desc")
            icon = icon_el.get_text(strip=True) if icon_el else ""
            lbl = label_el.get_text(strip=True) if label_el else ""
            desc = desc_el.get_text(strip=True) if desc_el else ""
            return f'\n- {icon} **{lbl}** — {desc}\n'

        # --- Formula → code ---
        if "formula" in classes:
            return f'\n`{el.get_text(strip=True)}`\n'

        # --- Summary item → inline stat ---
        if "summary-item" in classes:
            label_el = el.find("div", class_="label")
            value_el = el.find("div", class_="value")
            unit_el = el.find("div", class_="unit")
            lbl = label_el.get_text(strip=True) if label_el else ""
            val = value_el.get_text(strip=True) if value_el else ""
            unit = unit_el.get_text(strip=True) if unit_el else ""
            suffix = f" {unit}" if unit else ""
            return f'\n**{val}**{suffix} ({lbl})\n'

        # --- Status item → list item ---
        if "status-item" in classes:
            label_el = el.find("div", class_="label")
            value_el = el.find("div", class_="value")
            detail_el = el.find("div", class_="detail")
            lbl = label_el.get_text(strip=True) if label_el else ""
            val = value_el.get_text(strip=True) if value_el else ""
            det = detail_el.get_text(strip=True) if detail_el else ""
            detail = f" — {det}" if det else ""
            return f'\n- {lbl}: **{val}**{detail}\n'

        # --- Expert box → inline ---
        if "expert-box" in classes:
            return f' {el.get_text(strip=True)} '

        # --- Layer bar → inline ---
        if "layer-bar" in classes:
            return f' {el.get_text(strip=True)} '

        # --- Bucket item → list item ---
        if "bucket-item" in classes:
            name_el = el.find("div", class_="bucket-name")
            desc_el = el.find("div", class_="bucket-desc")
            name = name_el.get_text(strip=True) if name_el else ""
            desc = desc_el.get_text(" ", strip=True) if desc_el else ""
            return f'\n- `{name}` — {desc}\n'

        # --- Comp row → list item ---
        if "comp-row" in classes:
            label_el = el.find("span", class_="label")
            if label_el:
                lbl = label_el.get_text(strip=True)
                rest = el.get_text(" ", strip=True).replace(lbl, "", 1).strip()
                return f'\n- **{lbl}**: {rest}\n'

        # --- Rating → list ---
        if "rating" in classes:
            items = el.find_all("div", class_="rating-item")
            if items:
                lines = []
                for item in items:
                    lines.append(f'- {item.get_text(" ", strip=True)}')
                return '\n\n' + '\n'.join(lines) + '\n\n'

        # --- Mermaid diagrams → code block ---
        if "mermaid" in classes:
            raw = el.get_text()
            return f'\n```mermaid\n{raw.strip()}\n```\n'

        # --- Row with label/value spans → table row ---
        if "row" in classes:
            lbl = el.find("span", class_="lbl")
            val = el.find("span", class_="val")
            if lbl and val:
                return f"| {lbl.get_text(strip=True)} | {val.get_text(strip=True)} |\n"

        # --- Topology cells → code block (handled by parent) ---
        if "topo-cell" in classes:
            return f' {el.get_text(strip=True)} '

        # --- Topology grid → code block ---
        if any(c.startswith("topo") for c in classes) and el.find("div", class_=lambda x: x and "topo-cell" in (x if isinstance(x, list) else x.split())):
            raw = el.get_text()
            return f'\n```\n{raw.strip()}\n```\n'

        # --- Env label/value pairs → table row ---
        if "env-label" in classes or "env-value" in classes:
            return f' {el.get_text(strip=True)} '

        # --- Layer cell → inline ---
        if "layer-cell" in classes or "layer-row" in classes:
            return f' {el.get_text(strip=True)} '

        # --- Pipe row/block (pipeline diagrams) → code block ---
        if any(c.startswith("pipe") for c in classes):
            return f' {el.get_text(strip=True)} '

        # --- Generic: any div with many box-drawing chars ---
        raw = el.get_text()
        if len(_BOX_RE.findall(raw)) >= 5 and not el.find("div"):
            return f'\n```\n{raw.strip()}\n```\n'

        return f'\n{text}\n'

    def convert_p(self, el, text, parent_tags):
        """Detect ASCII art in <p> tags and wrap as code block."""
        raw = el.get_text()
        if len(_BOX_RE.findall(raw)) >= 3:
            return f'\n```\n{raw.strip()}\n```\n'
        return super().convert_p(el, text, parent_tags)

    def convert_svg(self, el, text, parent_tags):
        """Skip SVG elements — they can't render in markdown."""
        return ''

    def convert_style(self, el, text, parent_tags):
        """Skip style elements."""
        return ''

    def _is_in_table(self, el):
        """Check if element is inside a table."""
        parent = el.parent
        while parent:
            if parent.name in ('td', 'th', 'table'):
                return True
            parent = parent.parent
        return False

    def convert_a(self, el, text, parent_tags):
        href = el.get("href", "")
        classes = el.get("class", [])
        if isinstance(classes, str):
            classes = classes.split()

        # Wiki internal links → [[slug|text]] (but avoid | in tables)
        if "wiki-link" in classes and href:
            slug = self._extract_slug(href)
            if slug:
                display = text.strip()
                in_table = self._is_in_table(el)
                if display and display != slug and not in_table:
                    return f"[[{slug}|{display}]]"
                return f"[[{slug}]]"

        # Source reference links → standard markdown
        if "source-ref" in classes and href:
            return f"[{text.strip()}]({href})"

        # Regular external links
        if href and href.startswith(("http://", "https://")):
            return f"[{text.strip()}]({href})"

        # Fallback
        if href:
            return f"[{text.strip()}]({href})"
        return text

    def _extract_slug(self, href: str) -> str:
        """Extract slug from href like '../entities/tpu-v7.html' → 'tpu-v7'."""
        # Remove ../ prefixes and directory
        name = href.split("/")[-1]
        # Remove .html extension
        if name.endswith(".html"):
            name = name[:-5]
        return name


def convert_html(el, **kwargs):
    """Convert a BeautifulSoup element to Markdown using our custom converter."""
    return WikiMarkdownConverter(**kwargs).convert_soup(
        BeautifulSoup(str(el), "html.parser")
    )


def extract_metadata(soup: BeautifulSoup) -> dict:
    """Extract wiki metadata from HTML meta tags and header."""
    meta = {}

    # Meta tags
    for tag in soup.find_all("meta"):
        name = tag.get("name", "")
        content = tag.get("content", "")
        if name == "wiki-type":
            meta["type"] = content
        elif name == "wiki-tags":
            meta["tags"] = [t.strip() for t in content.split(",") if t.strip()]
        elif name == "wiki-created":
            meta["date"] = content
        elif name == "wiki-updated":
            meta["lastmod"] = content
        elif name == "wiki-sources":
            meta["sources"] = content
        elif name == "wiki-links-to":
            meta["links_to"] = [t.strip() for t in content.split(",") if t.strip()]

    # Title from h1
    h1 = soup.find("h1", attrs={"data-pagefind-meta": "title"})
    if h1:
        meta["title"] = h1.get_text(strip=True)

    # Summary from p.wiki-summary
    summary_p = soup.find("p", class_="wiki-summary")
    if summary_p:
        meta["description"] = summary_p.get_text(strip=True)

    return meta


def build_frontmatter(meta: dict) -> str:
    """Build YAML frontmatter from metadata dict."""
    lines = ["---"]
    if "title" in meta:
        # Escape quotes in title
        title = meta["title"].replace('"', '\\"')
        lines.append(f'title: "{title}"')
    if "description" in meta:
        desc = meta["description"].replace('"', '\\"')
        lines.append(f'description: "{desc}"')
    if "type" in meta:
        lines.append(f"type: {meta['type']}")
    if "date" in meta:
        lines.append(f"date: {meta['date']}")
    if "lastmod" in meta:
        lines.append(f"lastmod: {meta['lastmod']}")
    if "tags" in meta and meta["tags"]:
        lines.append("tags:")
        for tag in meta["tags"]:
            lines.append(f"  - {tag}")
    if "links_to" in meta and meta["links_to"]:
        lines.append("links:")
        for link in meta["links_to"]:
            lines.append(f"  - {link}")
    lines.append("---")
    return "\n".join(lines)


def convert_main_content(soup: BeautifulSoup) -> str:
    """Convert the <main> content to Markdown."""
    main = soup.find("main")
    if not main:
        return ""

    # Convert to markdown with our custom converter
    md = WikiMarkdownConverter(
        heading_style="atx",
        bullets="-",
        strong_em_symbol="*",
        escape_underscores=False,
        escape_asterisks=False,
        escape_misc=False,
    ).convert_soup(BeautifulSoup(str(main), "html.parser"))

    # Fix wikilinks inside tables: [[slug|display]] → [[slug]] to avoid breaking table pipes
    def _fix_wikilinks_in_tables(md_text):
        lines = md_text.split('\n')
        fixed = []
        for line in lines:
            if line.startswith('|') and '[[' in line and ']]' in line:
                # Replace [[slug|display]] with [[slug]] in table rows
                line = re.sub(r'\[\[([^\]|]+)\|([^\]]+)\]\]', r'[[\1]]', line)
            fixed.append(line)
        return '\n'.join(fixed)
    md = _fix_wikilinks_in_tables(md)

    # Fix broken tables: normalize column counts
    def _fix_table_columns(md_text):
        lines = md_text.split('\n')
        result = []
        i = 0
        while i < len(lines):
            # Detect table blocks
            if lines[i].startswith('|') and lines[i].rstrip().endswith('|'):
                table_start = i
                table_lines = []
                while i < len(lines) and lines[i].startswith('|') and lines[i].rstrip().endswith('|'):
                    table_lines.append(lines[i])
                    i += 1
                if len(table_lines) >= 2:
                    # Find max column count
                    max_cols = max(len(l.split('|')) - 2 for l in table_lines)
                    # Normalize each row
                    fixed_table = []
                    for tl in table_lines:
                        cols = tl.split('|')[1:-1]  # strip empty first/last
                        while len(cols) < max_cols:
                            cols.append(' ')
                        # Remove extra empty columns
                        while len(cols) > max_cols:
                            # Remove trailing empty columns
                            if cols[-1].strip() == '':
                                cols.pop()
                            else:
                                break
                        fixed_table.append('| ' + ' | '.join(c.strip() for c in cols[:max_cols]) + ' |')
                    # Fix separator row
                    for j, fl in enumerate(fixed_table):
                        if re.match(r'^\|[\s\-:|]+\|$', fl):
                            sep_cols = ['---'] * max_cols
                            fixed_table[j] = '| ' + ' | '.join(sep_cols) + ' |'
                    result.extend(fixed_table)
                else:
                    result.extend(table_lines)
            else:
                result.append(lines[i])
                i += 1
        return '\n'.join(result)
    md = _fix_table_columns(md)

    # Clean up excessive blank lines
    md = re.sub(r"\n{3,}", "\n\n", md)

    # Fix code block language markers on separate line: ```\nlang\n → ```lang\n
    _CODE_LANGS = r'(python|bash|shell|sh|yaml|yml|json|text|javascript|typescript|go|rust|cpp|java|sql|toml|xml|html|css|diff|makefile|dockerfile|ini|conf|nginx|hcl|terraform|protobuf|proto|lua|ruby|perl|scala|kotlin|swift|r|matlab|latex|tex|graphql|markdown|md|plaintext|console|powershell|ps1|vim|awk|sed|zsh|fish|log|csv|tsv)'
    md = re.sub(r'```\n' + _CODE_LANGS + r'\n', r'```\1\n', md)

    # Escape ALL dollar signs outside code blocks to prevent KaTeX rendering
    # Covers prices ($5.30), shell vars ($DISTRIBUTED_ARGS), units (GB/$)
    parts = re.split(r'(```[\s\S]*?```)', md)
    for idx in range(0, len(parts), 2):  # even indices = non-code parts
        if idx < len(parts):
            parts[idx] = re.sub(r'(?<!\\)\$', r'\\$', parts[idx])
    md = ''.join(parts)

    # Merge orphaned number+label pairs (stat card artifacts)
    # Two cases:
    #   1. Small integer (1-99) + bold label → ordered list: "1. **label**"
    #   2. Large number/stat + label → bold stat: "**number** label"
    lines = md.split('\n')
    merged = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if re.match(r'^\d[\d,.]*$', line) and i + 1 < len(lines):
            next_line = lines[i + 1].strip()
            if next_line and not next_line.startswith(('#', '|', '-', '*', '[', '>', '`')):
                # Small integer → ordered list item
                if re.match(r'^\d{1,2}$', line):
                    merged.append(f'{line}. {next_line}')
                else:
                    merged.append(f'**{line}** {next_line}')
                i += 2
                continue
            # Small integer + bold next line → ordered list
            elif next_line and next_line.startswith('**') and re.match(r'^\d{1,2}$', line):
                merged.append(f'{line}. {next_line}')
                i += 2
                continue
        merged.append(lines[i])
        i += 1
    md = '\n'.join(merged)

    # Remove consecutive same-level headers with no content between them
    # (artifacts from stripped SVG/diagram sections)
    lines = md.split('\n')
    cleaned = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Check if this is a header followed by blank lines then same-or-higher-level header
        if line.startswith('#') and not line.startswith('#!'):
            level = len(line) - len(line.lstrip('#'))
            # Look ahead past blank lines
            j = i + 1
            while j < len(lines) and lines[j].strip() == '':
                j += 1
            if j < len(lines) and lines[j].startswith('#'):
                next_level = len(lines[j]) - len(lines[j].lstrip('#'))
                # Same or higher level with only blank lines between → skip this header
                if next_level <= level and j <= i + 3:
                    i += 1
                    continue
        cleaned.append(line)
        i += 1
    md = '\n'.join(cleaned)

    return md.strip()


def convert_page(html_path: Path, output_dir: Path, dry_run: bool = False) -> bool:
    """Convert a single HTML wiki page to Markdown."""
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()

    soup = BeautifulSoup(html, "html.parser")
    meta = extract_metadata(soup)

    if not meta.get("title"):
        print(f"  SKIP (no title): {html_path.name}")
        return False

    # Determine output path
    # html_path is like ~/my-wiki/wiki/entities/tpu-v7.html
    # subdir is "entities", filename is "tpu-v7"
    subdir = html_path.parent.name  # sources, entities, concepts, analyses
    slug = html_path.stem  # tpu-v7

    out_path = output_dir / subdir / f"{slug}.md"

    if dry_run:
        print(f"  {subdir}/{slug}.html → {subdir}/{slug}.md  ({meta.get('title', '?')})")
        return True

    frontmatter = build_frontmatter(meta)
    content = convert_main_content(soup)

    md = f"{frontmatter}\n\n{content}\n"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(md)

    print(f"  ✓ {subdir}/{slug}.md")
    return True


def main():
    parser = argparse.ArgumentParser(description="Convert CC Wiki HTML to Quartz Markdown")
    parser.add_argument("pages", nargs="*", help="Specific pages to convert (e.g. sources/tpu-v7.html)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be converted")
    args = parser.parse_args()

    if args.pages:
        html_files = [WIKI_DIR / p for p in args.pages]
    else:
        html_files = []
        for subdir in SUBDIRS:
            pattern = WIKI_DIR / subdir / "*.html"
            html_files.extend(sorted(Path(p) for p in glob.glob(str(pattern))))

    print(f"Converting {len(html_files)} pages...")
    success = 0
    for html_path in html_files:
        if not html_path.exists():
            print(f"  NOT FOUND: {html_path}")
            continue
        if convert_page(html_path, OUTPUT_DIR, dry_run=args.dry_run):
            success += 1

    print(f"\nDone: {success}/{len(html_files)} pages converted")


if __name__ == "__main__":
    main()
