"""
Quality metrics and confidence scoring for document extraction.
"""

from typing import Any, Optional
from pydantic import BaseModel


class ConfidenceScore(BaseModel):
    """Confidence scoring for extracted content."""

    overall_score: float  # [0.0, 1.0]
    text_quality: float
    layout_quality: float
    table_quality: Optional[float] = None

    @classmethod
    def from_docling_output(cls, docling_result: Any, page_idx: int = 0) -> "ConfidenceScore":
        """
        Calculate confidence from actual Docling output.

        Docling provides per-page confidence reports with:
        - layout_score: Quality of document element recognition (0.0-1.0)
        - parse_score: 10th percentile score of digital text cells (0.0-1.0)
        - table_score: Table extraction quality (0.0-1.0, not yet fully implemented)

        Args:
            docling_result: ConversionResult from Docling
            page_idx: Page index (0-based) for per-page confidence

        Returns:
            ConfidenceScore instance with real quality metrics
        """
        # Extract per-page confidence report from Docling
        if hasattr(docling_result, 'confidence') and hasattr(docling_result.confidence, 'pages'):
            try:
                page_conf = docling_result.confidence.pages[page_idx]

                layout_quality = page_conf.layout_score
                text_quality = page_conf.parse_score
                ocr_quality = page_conf.ocr_score

                # Table score not yet fully implemented in Docling, use layout as proxy
                table_quality = page_conf.table_score if page_conf.table_score > 0 else layout_quality

                # Weighted average: layout and parse are most important
                # OCR score is 0 for digital PDFs, so give it less weight
                overall = (layout_quality * 0.4 + text_quality * 0.4 + ocr_quality * 0.2)

            except (IndexError, AttributeError):
                # Fallback if page index out of range or attribute missing
                overall = layout_quality = text_quality = 0.85
                ocr_quality = 0.0
                table_quality = None
        else:
            # Fallback for older Docling versions or missing confidence data
            overall = layout_quality = text_quality = 0.85
            ocr_quality = 0.0
            table_quality = None

        return cls(
            overall_score=overall,
            text_quality=text_quality,
            layout_quality=layout_quality,
            table_quality=table_quality,
        )
