"""
Tier 1: Docling layout parser.

Primary document extraction using Docling library.
"""

import threading
from pathlib import Path
from typing import List, Any
import time
import math
import logging
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions

# Try to import ContentLayer for header/footer extraction (Docling v1.0+)
try:
    from docling_core.types.doc.document import ContentLayer
    CONTENT_LAYER_AVAILABLE = True
except ImportError:
    CONTENT_LAYER_AVAILABLE = False

from docmark.models.document_schema import (
    PageResult,
    ContentBlock,
    BlockType,
    ExtractionMethod,
    TableContent,
    ImageContent,
)
from docmark.models.quality_metrics import ConfidenceScore


logger = logging.getLogger(__name__)

# Docling's DocumentConverter is not thread-safe for concurrent convert() calls.
# This lock serialises all Docling conversions across the process so concurrent
# jobs don't corrupt each other's ML-model state.
_DOCLING_LOCK = threading.Lock()


class DoclingParser:
    """
    Tier 1: Primary layout parser using Docling.

    Extracts text, tables, images, and document structure with confidence scoring.
    """

    def __init__(self, table_confidence_threshold: float = 0.90):
        """
        Initialize Docling parser.

        Args:
            table_confidence_threshold: Threshold for table extraction quality
        """
        # Configure PDF processing options with OCR disabled
        pipeline_options = PdfPipelineOptions(do_ocr=False)
        format_options = {InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)}
        self.converter = DocumentConverter(format_options=format_options)
        self.table_threshold = table_confidence_threshold

    def parse_document(self, pdf_path: Path) -> tuple[Any, List[PageResult]]:
        """
        Parse entire PDF document using Docling.

        Args:
            pdf_path: Path to PDF file

        Returns:
            Tuple of (ConversionResult with metadata, List of PageResult objects)
        """
        # Convert document — serialised across threads (Docling is not thread-safe).
        with _DOCLING_LOCK:
            result = self.converter.convert(str(pdf_path))

        blocks_by_page = None
        if CONTENT_LAYER_AVAILABLE:
            try:
                blocks_by_page = self._extract_blocks_grouped_by_page(result)
            except Exception as e:
                logger.warning(
                    f"Failed to use single-pass iterate_items() API: {e}. "
                    "Falling back to element-based approach."
                )

        pages: List[PageResult] = []

        # Process each page
        for page_num, page_content in enumerate(result.document.pages.values(), start=1):
            page_start = time.time()

            if blocks_by_page is not None:
                blocks = blocks_by_page.get(page_num, [])
            else:
                blocks = self._extract_blocks(page_content)

            # Calculate confidence from Docling's per-page confidence report
            # page_num is 1-indexed, but page_idx needs to be 0-indexed
            confidence = ConfidenceScore.from_docling_output(result, page_num - 1)

            # Fix NaN confidence: use sensible defaults
            if len(blocks) == 0:
                conf_value = 0.0  # No content = low confidence
            elif confidence.overall_score is not None and not math.isnan(confidence.overall_score):
                conf_value = confidence.overall_score
            else:
                conf_value = 0.85  # Default for successful Docling extraction

            # Check for low-confidence tables
            has_low_conf_tables = any(
                block.type == BlockType.TABLE
                and isinstance(block.content, TableContent)
                and block.content.extraction_confidence < self.table_threshold
                for block in blocks
            )

            page_result = PageResult(
                page_num=page_num,
                extraction_method=ExtractionMethod.DOCLING,
                confidence=conf_value,
                processing_time_seconds=time.time() - page_start,
                blocks=blocks,
                warnings=[],
                fallback_triggered=False,
            )

            if has_low_conf_tables:
                page_result.warnings.append(
                    f"Tables with confidence < {self.table_threshold} detected"
                )

            pages.append(page_result)

        return result, pages

    def _extract_blocks_grouped_by_page(self, result: Any) -> dict[int, List[ContentBlock]]:
        """Extract blocks once for the full document and group by page number."""
        blocks_by_page: dict[int, List[ContentBlock]] = {}
        doc = result.document

        for item, _ in doc.iterate_items(
            included_content_layers={ContentLayer.BODY, ContentLayer.FURNITURE}
        ):
            page_num = self._get_item_page_no(item)
            if page_num is None:
                continue

            blocks_by_page.setdefault(page_num, []).append(self._build_block_from_item(item))

        return blocks_by_page

    @staticmethod
    def _get_item_page_no(item: Any) -> int | None:
        """Extract first available page number from a Docling item provenance."""
        if not hasattr(item, "prov") or not item.prov:
            return None

        for prov in item.prov:
            if hasattr(prov, "page_no") and prov.page_no is not None:
                return int(prov.page_no)

        return None

    def _build_block_from_item(self, item: Any) -> ContentBlock:
        """Build a content block from a Docling iterate_items() element."""
        label = str(getattr(item, "label", "text")).lower()
        block_type = self._map_docling_type(label)

        if block_type == BlockType.TABLE:
            content = self._extract_table_content(item)
        elif block_type == BlockType.IMAGE:
            content = self._extract_image_content(item)
        else:
            content = getattr(item, "text", str(item))

        bbox = getattr(item, "bbox", None)

        return ContentBlock(
            type=block_type,
            content=content,
            level=getattr(item, "level", None) if block_type == BlockType.HEADING else None,
            bbox=bbox,
        )

    def _extract_blocks(self, page_content: Any) -> List[ContentBlock]:
        """
        Fallback: extract content blocks from a single Docling page object.
        Used when single-pass iterate_items() extraction is unavailable.
        """
        blocks: List[ContentBlock] = []

        if hasattr(page_content, "elements") and page_content.elements:
            return self._extract_blocks_from_elements(page_content.elements)
        if hasattr(page_content, "text") and page_content.text:
            blocks.append(ContentBlock(type=BlockType.TEXT, content=page_content.text))

        return blocks

    def _extract_blocks_from_elements(self, elements: List[Any]) -> List[ContentBlock]:
        """
        Fallback: Extract blocks from element list (old approach).
        Used when iterate_items() is not available.

        Args:
            elements: List of Docling elements

        Returns:
            List of ContentBlock objects
        """
        blocks: List[ContentBlock] = []

        # Iterate through Docling elements
        for element in elements:
            block_type = self._map_docling_type(element.label if hasattr(element, "label") else "text")

            if block_type == BlockType.TABLE:
                # Extract table with markdown representation
                markdown_content = (
                    element.export_to_markdown()
                    if hasattr(element, "export_to_markdown")
                    else str(element)
                )
                table_content = TableContent(
                    markdown=markdown_content,
                    caption=getattr(element, "caption", None),
                    extraction_confidence=getattr(element, "confidence", 0.85),
                    enhanced_by_vision=False,
                )
                content = table_content

            elif block_type == BlockType.IMAGE:
                # Extract image metadata
                content = ImageContent(
                    description=getattr(element, "alt_text", ""),
                    caption=getattr(element, "caption", None),
                    extraction_confidence=getattr(element, "confidence", 0.90),
                )

            else:
                # Text content
                content = element.text if hasattr(element, "text") else str(element)

            # Get bounding box if available
            bbox = None
            if hasattr(element, "bbox"):
                bbox = element.bbox

            block = ContentBlock(
                type=block_type,
                content=content,
                level=(
                    getattr(element, "level", None)
                    if block_type == BlockType.HEADING
                    else None
                ),
                bbox=bbox,
            )
            blocks.append(block)

        return blocks

    @staticmethod
    def _extract_table_content(item: Any) -> TableContent:
        """Extract table content from Docling item."""
        markdown_content = (
            item.export_to_markdown()
            if hasattr(item, "export_to_markdown")
            else str(item)
        )
        return TableContent(
            markdown=markdown_content,
            caption=getattr(item, "caption", None),
            extraction_confidence=getattr(item, "confidence", 0.85),
            enhanced_by_vision=False,
        )

    @staticmethod
    def _extract_image_content(item: Any) -> ImageContent:
        """Extract image content from Docling item."""
        return ImageContent(
            description=getattr(item, "alt_text", ""),
            caption=getattr(item, "caption", None),
            extraction_confidence=getattr(item, "confidence", 0.90),
        )

    @staticmethod
    def _map_docling_type(docling_type: str) -> BlockType:
        """
        Map Docling element types to our BlockType enum.

        Args:
            docling_type: Docling element type string

        Returns:
            Corresponding BlockType
        """
        docling_type_lower = docling_type.lower()

        mapping = {
            "title": BlockType.HEADING,
            "heading": BlockType.HEADING,
            "section_header": BlockType.HEADING,
            "paragraph": BlockType.TEXT,
            "text": BlockType.TEXT,
            "table": BlockType.TABLE,
            "figure": BlockType.IMAGE,
            "picture": BlockType.IMAGE,
            "image": BlockType.IMAGE,
            "code": BlockType.CODE,
            "list": BlockType.LIST,
            "list_item": BlockType.LIST,
            "page_header": BlockType.PAGE_HEADER,
            "page_footer": BlockType.PAGE_FOOTER,
            "page-header": BlockType.PAGE_HEADER,  # Alternative format
            "page-footer": BlockType.PAGE_FOOTER,  # Alternative format
        }

        return mapping.get(docling_type_lower, BlockType.TEXT)
