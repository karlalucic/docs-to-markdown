"""
Document schema definitions for the preprocessing pipeline output.
"""

from enum import Enum
from typing import List, Optional, Dict, Any, Union
from pydantic import BaseModel, Field


class BlockType(str, Enum):
    """Content block types."""
    HEADING = "heading"
    TEXT = "text"
    TABLE = "table"
    IMAGE = "image"
    CODE = "code"
    LIST = "list"
    PAGE_HEADER = "page_header"
    PAGE_FOOTER = "page_footer"


class ExtractionMethod(str, Enum):
    """Extraction methods corresponding to tiers."""
    DOCLING = "docling"
    VISION_MODEL = "vision_model"


class TableContent(BaseModel):
    """Table content with markdown representation."""
    markdown: str
    caption: Optional[str] = None
    extraction_confidence: float
    enhanced_by_vision: bool = False


class ImageContent(BaseModel):
    """Image content with description."""
    description: str
    caption: Optional[str] = None
    extraction_confidence: float


class ContentBlock(BaseModel):
    """A single content block within a page."""
    type: BlockType
    content: Union[str, TableContent, ImageContent]  # Type depends on block type
    level: Optional[int] = None  # For headings (H1=1, H2=2, etc.)
    bbox: Optional[List[float]] = None  # Bounding box [x1, y1, x2, y2]

    class Config:
        use_enum_values = True


class PageResult(BaseModel):
    """Result for a single page extraction."""
    page_num: int
    extraction_method: ExtractionMethod
    confidence: float
    processing_time_seconds: float
    blocks: List[ContentBlock]
    warnings: List[str] = Field(default_factory=list)
    fallback_triggered: bool = False

    class Config:
        use_enum_values = True


class ProcessingSummary(BaseModel):
    """Summary of processing statistics for 2-tier pipeline."""
    tier1_pages: int = 0
    tier3_pages: int = 0
    total_processing_time_seconds: float = 0.0
    total_api_calls: int = 0
    estimated_cost_usd: float = 0.0


class DocumentResult(BaseModel):
    """Complete document processing result."""
    doc_id: str
    filename: str
    total_pages: int
    processing_summary: ProcessingSummary
    pages: List[PageResult]
    overall_confidence: float

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return self.model_dump()

    class Config:
        use_enum_values = True
