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
    """Render a 1-indexed PDF page to a base64-encoded PNG.

    Child objects (page, bitmap) are explicitly closed in reverse creation order
    before the parent document is closed.  pypdfium2 does not automatically
    close children when the parent closes, so leaving them open would silently
    exhaust PDFium's internal handle pool across many sequential conversions —
    eventually causing ``get_pdf_page_count`` to fail with a "fail to load
    document" error even for valid PDFs.
    """
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        page = pdf[page_num - 1]
        try:
            scale = dpi / 72
            bitmap = page.render(scale=scale)
            try:
                pil_image = bitmap.to_pil()
            finally:
                bitmap.close()
        finally:
            page.close()
        buf = io.BytesIO()
        pil_image.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")
    finally:
        pdf.close()
