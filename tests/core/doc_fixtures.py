"""Tiny real documents for the /add tests, built at test time (no binaries in the repo).

Each builder writes one file and returns its path. Every text fixture holds a
known phrase, so a test can check the conversion kept it.
"""

from __future__ import annotations

import os
from pathlib import Path

PDF_PHRASE = "Quarterly seismic readings rose sharply"
DOCX_PHRASE = "The fetcher retries three times"
PPTX_PHRASE = "Storage layer roadmap"
XLSX_PHRASE = "alerts fire at or above this magnitude"

FILLER = ("The station network reports hourly. Each reading carries a timestamp, a depth, and a magnitude. "
          "Readings are kept for one year and summarized each quarter.")


def make_pdf(path: Path, pages: int = 2, title: str = "Seismic report") -> Path:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=A4)
    for page in range(1, pages + 1):
        c.setFont("Helvetica-Bold", 16)
        c.drawString(72, 780, f"{title}, page {page}")
        c.setFont("Helvetica", 11)
        c.drawString(72, 750, PDF_PHRASE if page == 1 else "Page two continues the readings.")
        for i, chunk in enumerate(range(0, len(FILLER), 90)):
            c.drawString(72, 720 - 16 * i, FILLER[chunk:chunk + 90])
        c.showPage()
    c.save()
    return path


def make_scanned_pdf(path: Path) -> Path:
    """One page that is only an image: no text layer, as a scanner would make."""
    from PIL import Image, ImageDraw
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    img = Image.new("RGB", (600, 300), "white")
    ImageDraw.Draw(img).text((20, 20), "A scanned page: these words are pixels, not text.", fill="black")
    c = canvas.Canvas(str(path), pagesize=A4)
    c.drawImage(ImageReader(img), 72, 400, width=450, height=225)
    c.showPage()
    c.save()
    return path


def make_docx(path: Path, heading: str = "Fetcher design") -> Path:
    import docx

    d = docx.Document()
    d.add_heading(heading, level=1)
    d.add_paragraph(DOCX_PHRASE + " with a one-second backoff before giving up.")
    d.add_paragraph(FILLER)
    d.save(str(path))
    return path


def make_pptx(path: Path) -> Path:
    from pptx import Presentation

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = PPTX_PHRASE
    slide.placeholders[1].text = "Embedded SQLite first; revisit when concurrent writers matter.\n" + FILLER
    prs.save(str(path))
    return path


def make_xlsx(path: Path) -> Path:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Settings"
    ws.append(["setting", "value", "notes"])
    ws.append(["magnitude_threshold", 4.5, XLSX_PHRASE])
    ws.append(["retention_days", 365, "one year of readings is kept"])
    ws.append(["stations", 12, "hourly reports from every station"])
    wb.save(str(path))
    return path


def make_corrupt_pdf(path: Path, size: int = 4096) -> Path:
    path.write_bytes(os.urandom(size))
    return path


def make_big(path: Path, size: int) -> Path:
    """A sparse file of ``size`` bytes: instant, and no disk used."""
    with open(path, "wb") as f:
        f.truncate(size)
    return path
