"""PDF utility functions backed by pypdfium2 (Apache-2.0 / BSD-3)."""

from pathlib import Path
import base64
import io

import pypdfium2 as pdfium


def get_pdf_page_count(pdf_path: Path) -> int:
    """Return the number of pages in a PDF."""
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        return len(pdf)
    finally:
        pdf.close()


def pdf_page_to_base64(pdf_path: Path, page_num: int, dpi: int = 200) -> str:
    """Render a 1-indexed PDF page to a base64-encoded PNG."""
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        page = pdf[page_num - 1]
        scale = dpi / 72
        pil_image = page.render(scale=scale).to_pil()
        buf = io.BytesIO()
        pil_image.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")
    finally:
        pdf.close()
