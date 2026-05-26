"""Helpers for filling docx templates while preserving formatting.

Import these in your fill script:
    from fill_helpers import set_para_text, set_cell_text, xml_replace
"""
import zipfile
import shutil
import tempfile


def set_para_text(p, text):
    """Replace paragraph text, keeping the first run's formatting (font/size/color)."""
    if not p.runs:
        p.add_run(text)
        return
    p.runs[0].text = text
    for r in p.runs[1:]:
        r.text = ""


def set_cell_text(cell, text):
    """Replace table cell text. Supports multi-line via \\n.

    Keeps the first paragraph's first run formatting; removes any extra paragraphs.
    """
    paras = cell.paragraphs
    if not paras:
        cell.add_paragraph(text)
        return
    # Drop extra paragraphs first
    for p in paras[1:]:
        p._element.getparent().remove(p._element)
    p = paras[0]
    lines = text.split("\n")
    set_para_text(p, lines[0])
    for line in lines[1:]:
        cell.add_paragraph(line)


def xml_replace(docx_path, replacements):
    """Raw XML find-and-replace inside a docx (in-place).

    Use this for content that python-docx CANNOT reach:
      - Text inside textboxes / shapes (e.g. cover-page titles)
      - Headers/footers that aren't picked up via sec.header.paragraphs
      - SDT (content controls)

    Args:
        docx_path: path to .docx file (will be modified in place)
        replacements: dict[str, str] of literal text replacements
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".docx", delete=False).name
    with zipfile.ZipFile(docx_path) as zin, \
         zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.namelist():
            data = zin.read(item)
            if item.endswith(".xml"):
                for old, new in replacements.items():
                    data = data.replace(old.encode("utf-8"),
                                        new.encode("utf-8"))
            zout.writestr(item, data)
    shutil.move(tmp, docx_path)
