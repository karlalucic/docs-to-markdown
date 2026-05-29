"""
Quality metrics and confidence scoring for document extraction.
"""

import math
from collections.abc import Mapping
from typing import Any, Optional
from pydantic import BaseModel


class ConfidenceScore(BaseModel):
    """Confidence scoring for extracted content."""

    overall_score: float  # [0.0, 1.0]
    text_quality: float
    layout_quality: float
    table_quality: Optional[float] = None

    @classmethod
    def from_docling_output(cls, docling_result: Any, page_num: int = 1) -> "ConfidenceScore":
        """
        Calculate confidence from actual Docling output.

        Docling provides per-page confidence reports with:
        - layout_score: Quality of document element recognition (0.0-1.0)
        - parse_score: 10th percentile score of digital text cells (0.0-1.0)
        - table_score: Table extraction quality (0.0-1.0, not yet fully implemented)

        Args:
            docling_result: ConversionResult from Docling
            page_num: Page number (1-based) for per-page confidence

        Returns:
            ConfidenceScore instance with real quality metrics
        """
        page_conf = None
        confidence = getattr(docling_result, "confidence", None)
        pages = getattr(confidence, "pages", None)

        if pages is not None:
            try:
                if isinstance(pages, Mapping):
                    page_conf = pages.get(page_num) or pages.get(page_num - 1)
                else:
                    page_conf = pages[page_num - 1]
            except (IndexError, AttributeError, KeyError, TypeError):
                page_conf = None

        if page_conf is None:
            overall = layout_quality = text_quality = 0.85
            table_quality = None
        else:
            layout_quality = _score(getattr(page_conf, "layout_score", None), 0.85)
            text_quality = _score(getattr(page_conf, "parse_score", None), 0.85)
            table_quality = _score(getattr(page_conf, "table_score", None), layout_quality)

            overall = _score(getattr(page_conf, "mean_score", None), None)
            if overall is None:
                scores = [s for s in (layout_quality, text_quality) if s is not None]
                overall = sum(scores) / len(scores) if scores else 0.85

        return cls(
            overall_score=overall,
            text_quality=text_quality,
            layout_quality=layout_quality,
            table_quality=table_quality,
        )


def _score(value: Any, fallback: Optional[float]) -> Optional[float]:
    """Return a finite 0..1 score, ignoring Docling NaN placeholders."""
    try:
        score = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(score):
        return fallback
    return max(0.0, min(1.0, score))
